"""Modbus polling loop and the shared client link.

The sync pymodbus client is owned by :class:`ModbusLink`; the poller
thread reads through it and the safety FSM writes through it, with a
lock serializing transactions.
"""
from __future__ import annotations

import logging
import threading
import time

from pymodbus.client import ModbusTcpClient

from ..mapfile import ModbusMap, decode, encode
from .events import EventBus
from .metrics import BridgeMetrics
from .state import Snapshot, SharedState, now_ms

log = logging.getLogger("hydragate.poller")


class ModbusLink:
    """Serializes Modbus access; builds snapshots from raw registers."""

    def __init__(self, map_: ModbusMap, host: str, port: int, unit_id: int, timeout: float = 1.0) -> None:
        self.map = map_
        self.unit_id = unit_id
        self.client = ModbusTcpClient(host, port=port, timeout=timeout, retries=0)
        self.lock = threading.Lock()
        self.last_latency_ms = 0.0

    def poll(self) -> Snapshot | None:
        try:
            t0 = time.perf_counter()
            with self.lock:
                rr = self.client.read_holding_registers(0, count=self.map.holding_count, device_id=self.unit_id)
                rc = self.client.read_coils(0, count=self.map.coil_count, device_id=self.unit_id)
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
            if rr.isError() or rc.isError():
                return None
            return self._snapshot(list(rr.registers), list(rc.bits))
        except Exception as exc:  # connection errors, timeouts
            log.debug("poll failed: %s", exc)
            return None

    def write_register(self, name: str, value: float) -> bool:
        reg = self.map.holding_by_name(name)
        try:
            with self.lock:
                rr = self.client.write_register(reg.addr, encode(reg, value), device_id=self.unit_id)
            return not rr.isError()
        except Exception as exc:
            log.warning("write %s failed: %s", name, exc)
            return False

    def _snapshot(self, regs: list[int], bits: list[bool]) -> Snapshot:
        m = self.map

        def reg(name: str) -> float:
            r = m.holding_by_name(name)
            return decode(r, regs[r.addr])

        leak = tuple(
            bool(bits[r.addr]) for r in m.coils if r.name.startswith("leak_zone_")
        )
        return Snapshot(
            ts_ms=now_ms(),
            t_secondary_supply_c=reg("secondary_supply_temp_c"),
            t_secondary_return_c=reg("secondary_return_temp_c"),
            t_primary_supply_c=reg("primary_supply_temp_c"),
            t_primary_return_c=reg("primary_return_temp_c"),
            flow_lpm=reg("secondary_flow_lpm"),
            dp_mbar=reg("secondary_dp_mbar"),
            pump_setpoint_pct=reg("pump_speed_setpoint_pct"),
            pump_actual_pct=reg("pump_speed_actual_pct"),
            valve_setpoint_pct=reg("valve_position_setpoint_pct"),
            valve_actual_pct=reg("valve_position_actual_pct"),
            heat_load_kw=reg("it_heat_load_kw"),
            alarm_word=int(reg("alarm_word")),
            status_word=int(reg("status_word")),
            leak_zones=leak,
        )


class Poller(threading.Thread):
    """Polls the CDU at a fixed interval and maintains shared state."""

    def __init__(
        self,
        link: ModbusLink,
        shared: SharedState,
        events: EventBus,
        metrics: BridgeMetrics,
        poll_interval_ms: int,
        stale_after_failures: int,
        cdu_id: str = "cdu-01",
        manager_id: str = "hydragate-01",
    ) -> None:
        super().__init__(daemon=True, name="poller")
        self.link = link
        self.shared = shared
        self.events = events
        self.metrics = metrics
        self.poll_ms = poll_interval_ms
        self.stale_after = stale_after_failures
        self.cdu_id = cdu_id
        self.manager_id = manager_id
        self._stop = threading.Event()
        self._zone_names: list[str] = []

    def set_zone_names(self, names: list[str]) -> None:
        self._zone_names = names

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            start = now_ms()
            snap = self.link.poll()
            if snap is not None:
                failures = 0
                if not self.shared.comms_ok:
                    self.shared.set_comms(True)
                    self.metrics.set_comms(True)
                    self.events.publish(
                        "CommunicationRestored", "OK",
                        origin=f"/redfish/v1/Managers/{self.manager_id}",
                        message="Modbus telemetry restored",
                    )
                self.shared.update(snap)
                self.metrics.update_snapshot(snap, self._zone_names)
                self.metrics.observe_poll(True, self.link.last_latency_ms)
            else:
                failures += 1
                self.metrics.observe_poll(False, 0.0)
                if failures >= self.stale_after and self.shared.comms_ok:
                    self.shared.set_comms(False)
                    self.metrics.set_comms(False)
                    self.events.publish(
                        "CommunicationLost", "Critical",
                        origin=f"/redfish/v1/CoolingUnits/{self.cdu_id}",
                        message=f"Modbus telemetry lost after {failures} failed polls - data marked stale",
                        consecutive_failures=failures,
                    )
            elapsed = now_ms() - start
            self._stop.wait(max(0, self.poll_ms - elapsed) / 1000.0)
