"""Thread-safe shared state: latest snapshot plus telemetry history."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Snapshot:
    ts_ms: int
    t_secondary_supply_c: float
    t_secondary_return_c: float
    t_primary_supply_c: float
    t_primary_return_c: float
    flow_lpm: float
    dp_mbar: float
    pump_setpoint_pct: float
    pump_actual_pct: float
    valve_setpoint_pct: float
    valve_actual_pct: float
    heat_load_kw: float
    alarm_word: int
    status_word: int
    leak_zones: tuple[bool, ...]

    @property
    def delta_t_k(self) -> float:
        return self.t_secondary_return_c - self.t_secondary_supply_c

    def to_dict(self) -> dict:
        d = {
            "ts_ms": self.ts_ms,
            "timestamp": _iso(self.ts_ms),
            "secondary_supply_temp_c": round(self.t_secondary_supply_c, 2),
            "secondary_return_temp_c": round(self.t_secondary_return_c, 2),
            "primary_supply_temp_c": round(self.t_primary_supply_c, 2),
            "primary_return_temp_c": round(self.t_primary_return_c, 2),
            "secondary_flow_lpm": round(self.flow_lpm, 2),
            "secondary_dp_mbar": round(self.dp_mbar, 1),
            "pump_setpoint_pct": self.pump_setpoint_pct,
            "pump_actual_pct": self.pump_actual_pct,
            "valve_setpoint_pct": self.valve_setpoint_pct,
            "valve_actual_pct": self.valve_actual_pct,
            "heat_load_kw": self.heat_load_kw,
            "delta_t_k": round(self.delta_t_k, 2),
            "alarm_word": self.alarm_word,
            "status_word": self.status_word,
            "leak_zones": list(self.leak_zones),
        }
        return d


def _iso(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + f".{ts_ms % 1000:03d}Z"


class SharedState:
    """Holds the latest telemetry snapshot, a history ring and status flags.

    Written by the poller and the FSM, read by the API layer.
    """

    def __init__(self, history_len: int = 1200) -> None:
        self._lock = threading.Lock()
        self._history: deque[Snapshot] = deque(maxlen=history_len)
        self.latest: Snapshot | None = None
        self.comms_ok = True
        self.fsm_state = "NORMAL"
        self.fsm_entered_ms = now_ms()
        self.started_ms = now_ms()

    def update(self, snap: Snapshot) -> None:
        with self._lock:
            self.latest = snap
            self._history.append(snap)

    @property
    def latest_unlocked(self) -> Snapshot | None:
        return self.latest

    def history_since(self, ts_ms: int) -> list[Snapshot]:
        with self._lock:
            return [s for s in self._history if s.ts_ms >= ts_ms]

    def history_last(self, seconds: float) -> list[Snapshot]:
        return self.history_since(now_ms() - int(seconds * 1000))

    def set_fsm(self, state: str) -> None:
        with self._lock:
            self.fsm_state = state
            self.fsm_entered_ms = now_ms()

    def set_comms(self, ok: bool) -> None:
        with self._lock:
            self.comms_ok = ok

    def fsm_info(self) -> dict:
        with self._lock:
            return {
                "state": self.fsm_state,
                "entered_ms": self.fsm_entered_ms,
                "in_state_s": round((now_ms() - self.fsm_entered_ms) / 1000.0, 2),
            }
