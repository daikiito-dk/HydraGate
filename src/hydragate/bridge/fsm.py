"""Water-hammer-aware safety interlock FSM.

Implements the machine defined in spec/fsm_contract.yaml. On leak
detection it coordinates pump ramp-down with staged valve closure so
that the Joukowsky surge implied by every flow change stays inside the
configured envelope:

    dP = rho * c * dv      (Joukowsky)

Shutdown sequence (deliberately NOT an instant slam):

1. RESPONDING      - first corrective write within one tick: cut pump
                     setpoint (momentum bleed), emit LeakDetected.
2. STAGED_ISOLATION- close the valve in steps; each step is sized so the
                     predicted flow change stays inside the surge budget,
                     then the flow is given a settle window before the
                     next stage.
3. ISOLATED        - valve fully closed, pump stopped; emit IsolationComplete
                     and a recommendation to de-energize affected racks.

A continuously-running hammer guard audits measured telemetry: any flow
change implying a surge above the limit raises UnsafeClosureDetected,
regardless of who caused it (this FSM, an operator, or a rogue controller).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass

from .config import BridgeConfig, CduSpec
from .events import EventBus
from .hammer import HammerParams, window_flow_change
from .metrics import BridgeMetrics
from .poller import ModbusLink
from .state import SharedState, now_ms

log = logging.getLogger("hydragate.fsm")

TICK_S = 0.05                 # FSM evaluation period
STAGE_MARGIN = 0.9            # fraction of the allowed flow step used per stage
FLOW_STABLE_LPM = 5.0         # window flow change below which the loop counts as settled
COMM_LOSS_FAULT_GRACE_S = 5.0
WARN_INTERVAL_MS = 10_000


@dataclass
class Action:
    ts_ms: int
    action: str
    detail: str
    predicted_delta_flow_lpm: float | None = None
    measured_delta_flow_lpm: float | None = None


class InterlockFSM(threading.Thread):
    def __init__(
        self,
        shared: SharedState,
        link: ModbusLink,
        events: EventBus,
        metrics: BridgeMetrics,
        cfg: BridgeConfig,
        hammer: HammerParams,
        cdu: CduSpec,
    ) -> None:
        super().__init__(daemon=True, name="fsm")
        self.shared = shared
        self.link = link
        self.events = events
        self.metrics = metrics
        self.cfg = cfg
        self.hammer = hammer
        self.cdu = cdu

        self.actions: list[Action] = []
        self.leak_detected_ms: int | None = None
        self._leak_latched = False
        self._guard_flagged = False
        self._prev_state = "NORMAL"
        self._pump_target: float | None = None
        self._pump_stopped = False
        self._last_action_flow: float | None = None
        self._stage_deadline = 0
        self._warned: dict[str, int] = {}
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- helpers
    def _manager_origin(self) -> str:
        return f"/redfish/v1/Managers/{self.cfg.manager_id}"

    def _detectors(self) -> list:
        return sorted(self.cdu.leak_detectors, key=lambda d: d.coil)

    def _set_state(self, new: str, **data) -> None:
        old = self.shared.fsm_state
        if new == old:
            return
        self.shared.set_fsm(new)
        self.metrics.set_fsm(new)
        self.events.publish(
            "StateChange", "Warning", origin=self._manager_origin(),
            message=f"safety interlock: {old} -> {new}",
            from_state=old, to_state=new, **data,
        )

    def _log_action(self, action: str, detail: str, predicted: float | None) -> None:
        a = Action(ts_ms=now_ms(), action=action, detail=detail, predicted_delta_flow_lpm=predicted)
        self.actions.append(a)
        self.events.publish(
            "InterlockAction", "OK", origin=self._manager_origin(), message=detail, **asdict(a)
        )

    def _warn_once(self, key: str, message_id: str, message: str, **data) -> None:
        now = now_ms()
        if now - self._warned.get(key, 0) > WARN_INTERVAL_MS:
            self._warned[key] = now
            self.events.publish(
                message_id, "Warning",
                origin=f"/redfish/v1/CoolingUnits/{self.cdu.id}", message=message, **data
            )

    # ------------------------------------------------------------- main loop
    def run(self) -> None:
        while not self._stop.is_set():
            snap = self.shared.latest
            if self.shared.comms_ok:
                self._step(snap)
            else:
                self._handle_comms_lost()
            self._stop.wait(TICK_S)

    def _guard(self, snap) -> None:
        """Audit measured telemetry against the Joukowsky envelope."""
        history = self.shared.history_last(2.0)
        if len(history) < 2:
            return
        # cold start: wait for at least two settle windows of baseline before
        # judging any window, so a gateway boot never cries wolf
        if (snap.ts_ms - history[0].ts_ms) < 2 * self.hammer.window_ms:
            return
        pair = window_flow_change(history, snap, self.hammer.window_ms)
        if pair is None:
            return
        surge = self.hammer.surge_pa(*pair)
        self.metrics.set_surge(surge)
        if surge > self.hammer.max_surge_pressure_pa:
            if not self._guard_flagged:
                self._guard_flagged = True
                self.events.publish(
                    "UnsafeClosureDetected", "Critical",
                    origin=f"/redfish/v1/CoolingUnits/{self.cdu.id}",
                    message=(
                        f"water-hammer surge {surge / 1e5:.2f} bar exceeds the "
                        f"{self.hammer.max_surge_pressure_pa / 1e5:.2f} bar limit"
                    ),
                    surge_pa=round(surge, 1), limit_pa=self.hammer.max_surge_pressure_pa,
                )
        elif self._guard_flagged and surge < 0.8 * self.hammer.max_surge_pressure_pa:
            self._guard_flagged = False

    def _step(self, snap) -> None:
        state = self.shared.fsm_state
        if snap is not None:
            self._guard(snap)
        if state == "COMMS_LOST":
            self._set_state(self._prev_state or "NORMAL")
            return
        if snap is None:
            return

        leaks = [i for i, on in enumerate(snap.leak_zones) if on]

        if state == "NORMAL":
            self._monitor_warnings(snap)
            if leaks:
                self._respond(snap, leaks)
        elif state == "RESPONDING":
            if self._pump_target is None or abs(snap.pump_actual_pct - self._pump_target) < 5.0:
                self._stage_deadline = now_ms() + self.cfg.safety.control.valve_step_settle_ms
                self._set_state("STAGED_ISOLATION")
        elif state == "STAGED_ISOLATION":
            self._stage_valve(snap)
        elif state == "ISOLATED":
            # interlock stays latched until an operator intervenes / process restart
            pass

        self._check_timeout()

    # ------------------------------------------------------------- states
    def _respond(self, snap, leaks: list[int]) -> None:
        self._leak_latched = True
        self.leak_detected_ms = now_ms()
        dets = self._detectors()
        names = [dets[i].id if i < len(dets) else f"zone-{i}" for i in leaks]
        self.events.publish(
            "LeakDetected", "Critical",
            origin=f"/redfish/v1/LeakDetectors/{names[0]}",
            message=f"coolant leak detected: {', '.join(names)}",
            zones=names,
        )
        ctrl = self.cfg.safety.control
        pump_now = snap.pump_actual_pct
        target = max(0.0, min(pump_now - ctrl.pump_cut_pct, ctrl.pump_cap_pct))
        if self.link.write_register("pump_speed_setpoint_pct", target):
            self._log_action(
                "pump_cut", f"pump {pump_now:.0f}% -> {target:.0f}% (momentum bleed)",
                predicted=(pump_now - target) * 2.0,
            )
        self._pump_target = target
        self._set_state("RESPONDING")

    def _stage_valve(self, snap) -> None:
        now = now_ms()
        if now < self._stage_deadline:
            return
        # the loop must be quasi-steady before the next stage: the pump ramp
        # tail and a valve step must never share one settle window, or their
        # combined dv could breach the Joukowsky envelope
        hist = self.shared.history_last(1.0)
        pair = window_flow_change(hist, snap, self.hammer.window_ms)
        if pair is not None and abs(pair[0] - pair[1]) > FLOW_STABLE_LPM:
            return

        ctrl = self.cfg.safety.control
        flow, valve = snap.flow_lpm, snap.valve_actual_pct

        # audit the previous stage now that its window has settled
        if self._last_action_flow is not None and self.actions:
            self.actions[-1].measured_delta_flow_lpm = round(self._last_action_flow - flow, 1)
            self._last_action_flow = None

        max_step = self.hammer.max_flow_step_lpm * STAGE_MARGIN

        if valve > 2.0:
            if flow <= max_step:
                new_valve, predicted = 0.0, flow
            else:
                new_flow = flow - max_step
                ratio = (new_flow / flow) if flow > 0 else 0.0
                new_valve = min(valve * ratio * ratio, valve - ctrl.min_valve_step_pct)
                new_valve = max(0.0, new_valve)
                predicted = flow - new_flow
            if self.link.write_register("valve_position_setpoint_pct", new_valve):
                self._log_action(
                    "valve_stage",
                    f"valve {valve:.0f}% -> {new_valve:.0f}% at flow {flow:.0f} lpm",
                    round(predicted, 1),
                )
                self._last_action_flow = flow
                self._stage_deadline = now + ctrl.valve_step_settle_ms
        elif snap.pump_actual_pct > 10.0:
            if not self._pump_stopped:
                self._pump_stopped = True
                if self.link.write_register("pump_speed_setpoint_pct", 0.0):
                    self._log_action("pump_stop", "valve closed, pump -> 0%", 0.0)
                    self._stage_deadline = now + ctrl.valve_step_settle_ms
        else:
            elapsed = (now - (self.leak_detected_ms or now)) / 1000.0
            self._set_state("ISOLATED")
            self.events.publish(
                "IsolationComplete", "OK",
                origin=f"/redfish/v1/CoolingUnits/{self.cdu.id}",
                message=f"loop isolated in {elapsed:.1f} s",
                elapsed_s=round(elapsed, 2),
            )
            self.events.publish(
                "MaintenanceRecommended", "Warning", origin=self._manager_origin(),
                message="de-energize affected IT racks and dispatch a leak inspection",
            )

    def _monitor_warnings(self, snap) -> None:
        th = self.cfg.thresholds
        if snap.t_secondary_return_c > th.secondary_return_temp_warning_c:
            self._warn_once(
                "high_return", "ThermalWarning",
                f"secondary return {snap.t_secondary_return_c:.1f} degC exceeds "
                f"{th.secondary_return_temp_warning_c} degC",
            )
        if snap.delta_t_k > th.delta_t_warning_k:
            self._warn_once(
                "high_dt", "ThermalWarning",
                f"secondary delta-T {snap.delta_t_k:.1f} K exceeds {th.delta_t_warning_k} K",
            )
        if (
            snap.pump_actual_pct > 10.0
            and snap.valve_actual_pct > 10.0
            and snap.flow_lpm < th.min_flow_lpm_warning
        ):
            self._warn_once(
                "low_flow", "FlowWarning",
                f"secondary flow {snap.flow_lpm:.0f} lpm below {th.min_flow_lpm_warning} lpm",
            )

    def _check_timeout(self) -> None:
        state = self.shared.fsm_state
        if state in ("RESPONDING", "STAGED_ISOLATION"):
            elapsed_s = (now_ms() - self.shared.fsm_entered_ms) / 1000.0
            if elapsed_s > self.cfg.safety.control.isolation_timeout_s:
                self._set_state("FAULT")
                self.events.publish(
                    "InterlockFault", "Critical", origin=self._manager_origin(),
                    message=(
                        f"safe shutdown did not complete within "
                        f"{self.cfg.safety.control.isolation_timeout_s:.0f} s"
                    ),
                )

    def _handle_comms_lost(self) -> None:
        state = self.shared.fsm_state
        if state != "COMMS_LOST":
            self._prev_state = state
            self._set_state("COMMS_LOST")
            return
        in_state_s = (now_ms() - self.shared.fsm_entered_ms) / 1000.0
        if (
            self._leak_latched
            and self._prev_state in ("RESPONDING", "STAGED_ISOLATION")
            and in_state_s > COMM_LOSS_FAULT_GRACE_S
        ):
            self._set_state("FAULT")
            self.events.publish(
                "InterlockFault", "Critical", origin=self._manager_origin(),
                message="safe shutdown incomplete when telemetry was lost",
            )
