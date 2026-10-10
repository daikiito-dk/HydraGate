"""Executable validation contracts (spec/contracts/*.yaml).

Each contract drives the live sim + bridge stack, injects a fault and
asserts the Redfish event plane, the safety interlock FSM and the
water-hammer envelope. These tests ARE the pass/fail criteria defined
in Phase 1 of the development plan.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from hydragate.bridge.hammer import HammerParams, window_flow_change
from hydragate.bridge.state import Snapshot

REPO = Path(__file__).resolve().parents[1]
CONTRACTS = sorted((REPO / "spec" / "contracts").glob("*.yaml"))


class SseCollector:
    """Background reader for the bridge's Server-Sent Events stream."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.events: list[tuple[float, str, dict]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        import httpx

        try:
            with httpx.Client(timeout=None) as client, client.stream("GET", self.url) as resp:
                event_name = None
                data_lines: list[str] = []
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        break
                    if line.startswith("event: "):
                        event_name = line[len("event: "):].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[len("data: "):])
                    elif line == "" and event_name is not None:
                        try:
                            data = json.loads("".join(data_lines))
                        except Exception:
                            data = {}
                        with self._lock:
                            self.events.append((time.time(), event_name, data))
                        event_name, data_lines = None, []
        except Exception:
            pass  # stream closed

    def wait_for(self, message_id: str, timeout_s: float) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._lock:
                for _, name, data in self.events:
                    if name == message_id:
                        return data
            time.sleep(0.02)
        with self._lock:
            seen = [name for _, name, _ in self.events]
        raise AssertionError(f"SSE event {message_id} not received within {timeout_s}s; saw: {seen[-10:]}")

    def stop(self) -> None:
        self._stop.set()


def _snapshot_from_dict(d: dict) -> Snapshot:
    return Snapshot(
        ts_ms=d["ts_ms"],
        t_secondary_supply_c=d["secondary_supply_temp_c"],
        t_secondary_return_c=d["secondary_return_temp_c"],
        t_primary_supply_c=d["primary_supply_temp_c"],
        t_primary_return_c=d["primary_return_temp_c"],
        flow_lpm=d["secondary_flow_lpm"],
        dp_mbar=d["secondary_dp_mbar"],
        pump_setpoint_pct=d["pump_setpoint_pct"],
        pump_actual_pct=d["pump_actual_pct"],
        valve_setpoint_pct=d["valve_setpoint_pct"],
        valve_actual_pct=d["valve_actual_pct"],
        heat_load_kw=d["heat_load_kw"],
        alarm_word=d["alarm_word"],
        status_word=d["status_word"],
        leak_zones=tuple(d["leak_zones"]),
    )


def _check_hammer(stack, hammer_exp: dict) -> None:
    """Independently recompute the max Joukowsky surge from telemetry history."""
    fsm = stack.get("/internal/fsm").json()["hammer"]
    params = HammerParams(
        rho_kg_m3=fsm["rho_kg_m3"],
        wave_speed_m_s=fsm["wave_speed_m_s"],
        pipe_inner_diameter_mm=fsm["pipe_inner_diameter_mm"],
        max_surge_pressure_pa=fsm["max_surge_pressure_pa"],
        window_ms=fsm["window_ms"],
    )
    samples = stack.get("/internal/history?seconds=90").json()["samples"]
    history = [_snapshot_from_dict(s) for s in samples]
    assert len(history) > 10, "telemetry history missing"

    max_surge = 0.0
    for snap in history:
        pair = window_flow_change(history, snap, params.window_ms)
        if pair is not None:
            max_surge = max(max_surge, params.surge_pa(*pair))

    limit = params.max_surge_pressure_pa * float(hammer_exp.get("tolerance", 1.0))
    assert max_surge <= limit, (
        f"Joukowsky surge {max_surge / 1e5:.2f} bar exceeds the "
        f"{limit / 1e5:.2f} bar envelope"
    )


def _run_step(step: dict, stack, sse: SseCollector) -> None:
    exp = step.get("expect", {})
    trigger_s: float | None = None

    action = step.get("action")
    if action:
        trigger_s = stack.inject(action["chaos"], action.get("args", {}))

    within_s = float(exp.get("within_s", 20.0))

    # 1. SSE latency checks (blocking, budgeted from the trigger instant)
    for entry in exp.get("sse", []):
        data = sse.wait_for(entry["message_id"], timeout_s=max(within_s, 5.0))
        if trigger_s is not None:
            trigger_ms = trigger_s * 1000.0
            latency_ms = max(0.0, data.get("timestamp_ms", 0) - trigger_ms)
            assert latency_ms <= entry["within_ms"], (
                f"{entry['message_id']} latency {latency_ms:.0f} ms exceeds "
                f"{entry['within_ms']} ms budget"
            )

    # 2. poll-based checks: interlock state + expected events
    deadline = time.time() + within_s
    seen: set[str] = set()
    fsm_state = None
    wanted_events = exp.get("events_contain", [])
    wanted_state = exp.get("interlock_state")

    while time.time() < deadline:
        fsm_state = stack.get("/internal/fsm").json()["state"]
        for event in stack.get("/internal/events?n=250").json()["events"]:
            seen.add(event["messageId"])
        if wanted_state is not None and fsm_state == wanted_state and all(
            e in seen for e in wanted_events
        ):
            break
        time.sleep(0.1)

    if wanted_state is not None:
        assert fsm_state == wanted_state, (
            f"interlock did not reach {wanted_state} within {within_s}s (last: {fsm_state})"
        )
    missing = [e for e in wanted_events if e not in seen]
    assert not missing, f"missing events: {missing}; saw: {sorted(seen)}"

    # 3. settle, then negative + invariant checks
    time.sleep(0.8)
    events = stack.get("/internal/events?n=250").json()["events"]
    ids = {e["messageId"] for e in events}
    for absent in exp.get("events_absent", []):
        assert absent not in ids, f"unexpected event observed: {absent}"

    if "end_state" in exp:
        unit = stack.get("/redfish/v1/CoolingUnits/cdu-01").json()
        sec = unit["SecondaryLoop"]
        end = exp["end_state"]
        assert sec["ValvePositionPct"] <= end["valve_pct_max"], (
            f"valve {sec['ValvePositionPct']}% not closed below {end['valve_pct_max']}%"
        )
        assert sec["PumpSpeedPct"] <= end["pump_pct_max"], (
            f"pump {sec['PumpSpeedPct']}% not below {end['pump_pct_max']}%"
        )

    if "telemetry_stale" in exp:
        unit = stack.get("/redfish/v1/CoolingUnits/cdu-01").json()
        assert unit["TelemetryStale"] == exp["telemetry_stale"]

    if "hammer" in exp:
        _check_hammer(stack, exp["hammer"])

    # 4. action latency (e.g. first corrective write after LeakDetected)
    for entry in exp.get("action_latency", []):
        anchor = next(e for e in events if e["messageId"] == entry["after_message_id"])
        target = next(
            e
            for e in events
            if e["messageId"] == "InterlockAction" and e["data"].get("action") == entry["action"]
        )
        latency_ms = target["data"]["ts_ms"] - anchor["timestamp_ms"]
        assert latency_ms <= entry["within_ms"], (
            f"{entry['action']} fired {latency_ms} ms after {entry['after_message_id']}, "
            f"budget {entry['within_ms']} ms"
        )


@pytest.mark.parametrize("contract_path", CONTRACTS, ids=lambda p: p.stem)
def test_contract(contract_path: Path, stack) -> None:
    spec = yaml.safe_load(contract_path.read_text())
    assert "steps" in spec and spec["steps"], f"contract {contract_path.name} has no steps"

    sse = SseCollector(f"{stack.base}/redfish/v1/EventService/SSE")
    try:
        # let the collector connect before any fault injection
        time.sleep(1.0)
        for step in spec["steps"]:
            _run_step(step, stack, sse)
    finally:
        sse.stop()
