"""Prometheus metrics for the bridge (own registry, test-friendly)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

from .state import Snapshot

FSM_STATES = ["NORMAL", "RESPONDING", "STAGED_ISOLATION", "ISOLATED", "COMMS_LOST", "FAULT"]


class BridgeMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        g = lambda name, doc: Gauge(name, doc, registry=self.registry)  # noqa: E731
        self.temp_secondary_supply = g("hydragate_secondary_supply_temp_c", "TCS supply temperature (degC)")
        self.temp_secondary_return = g("hydragate_secondary_return_temp_c", "TCS return temperature (degC)")
        self.temp_primary_supply = g("hydragate_primary_supply_temp_c", "FWS supply temperature (degC)")
        self.temp_primary_return = g("hydragate_primary_return_temp_c", "FWS return temperature (degC)")
        self.flow = g("hydragate_secondary_flow_lpm", "Secondary loop flow (L/min)")
        self.dp = g("hydragate_secondary_dp_mbar", "Secondary differential pressure (mbar)")
        self.pump_pct = g("hydragate_pump_speed_pct", "Pump speed (pct)")
        self.valve_pct = g("hydragate_valve_position_pct", "Valve position (pct)")
        self.heat_kw = g("hydragate_heat_load_kw", "Simulated IT heat load (kW)")
        self.delta_t = g("hydragate_delta_t_k", "Secondary loop delta-T (K)")
        self.comms_up = g("hydragate_modbus_up", "1 if Modbus telemetry is fresh")
        self.poll_latency = g("hydragate_poll_latency_ms", "Last Modbus poll latency (ms)")
        self.surge_pa = g("hydragate_surge_pressure_pa", "Joukowsky surge over trailing window (Pa)")
        self.surge_max_pa = g("hydragate_surge_pressure_max_pa", "Peak surge seen since bridge start (Pa)")

        self._fsm = Gauge(
            "hydragate_fsm_state", "FSM state indicator (1 = active)", ["state"], registry=self.registry
        )
        for state in FSM_STATES:  # pre-create every series so counters exist from t0
            self._fsm.labels(state=state).set(0)
        self.leak_zone = Gauge("hydragate_leak_zone", "Leak detector state (1 = leak)", ["zone"], registry=self.registry)
        self.polls = Counter("hydragate_polls_total", "Modbus polls", ["result"], registry=self.registry)
        self.events = Counter("hydragate_events_total", "Events published", ["message_id"], registry=self.registry)

        self._max_surge = 0.0

    def update_snapshot(self, snap: Snapshot, zone_names: list[str]) -> None:
        self.temp_secondary_supply.set(snap.t_secondary_supply_c)
        self.temp_secondary_return.set(snap.t_secondary_return_c)
        self.temp_primary_supply.set(snap.t_primary_supply_c)
        self.temp_primary_return.set(snap.t_primary_return_c)
        self.flow.set(snap.flow_lpm)
        self.dp.set(snap.dp_mbar)
        self.pump_pct.set(snap.pump_actual_pct)
        self.valve_pct.set(snap.valve_actual_pct)
        self.heat_kw.set(snap.heat_load_kw)
        self.delta_t.set(snap.delta_t_k)
        for i, name in enumerate(zone_names):
            if i < len(snap.leak_zones):
                self.leak_zone.labels(zone=name).set(1 if snap.leak_zones[i] else 0)

    def observe_poll(self, ok: bool, latency_ms: float) -> None:
        self.polls.labels(result="ok" if ok else "fail").inc()
        if ok:
            self.poll_latency.set(latency_ms)

    def set_comms(self, ok: bool) -> None:
        self.comms_up.set(1 if ok else 0)

    def set_fsm(self, state: str) -> None:
        for name in FSM_STATES:
            self._fsm.labels(state=name).set(1 if name == state else 0)

    def inc_event(self, message_id: str) -> None:
        self.events.labels(message_id=message_id).inc()

    def set_surge(self, pa: float) -> None:
        self.surge_pa.set(pa)
        if pa > self._max_surge:
            self._max_surge = pa
            self.surge_max_pa.set(pa)

    def expose(self) -> bytes:
        return generate_latest(self.registry)
