"""Virtual CDU Modbus TCP server: thermohydraulic loop + register mirror.

Architecture:
- ``SimState`` is the single source of truth for the machine.
- Client writes (pump/valve setpoints, fault injection) are captured by a
  write hook on the data blocks and applied to ``SimState`` directly.
- Every physics tick publishes a noisy sensor image into the datastore,
  so Modbus clients read exactly what the physics produces.
- While ``comm_blackout`` is engaged, reads raise a Modbus exception
  (sensor bus failure) while writes still land, so the fault can be
  cleared remotely.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
from pymodbus.exceptions import ModbusException
from pymodbus.server import StartAsyncTcpServer

from ..mapfile import ModbusMap, decode, encode
from . import physics as ph
from .physics import SimParams, SimState

log = logging.getLogger("hydragate.sim")


class CduDataBlock(ModbusSequentialDataBlock):
    """Data block mirroring the physics state.

    The pymodbus server uses the ``async_*`` methods, while the physics
    publisher uses the sync ones - both paths must behave identically:
    reads fail while the sensor bus is in blackout, writes are captured
    by the hook (except when flagged as a local publish).
    """

    def __init__(self, address, values, *, state: SimState, kind: str, on_write):
        self._state = state
        self._kind = kind  # "holding" | "coils" - address spaces overlap
        self._on_write = on_write
        self.publishing = False
        self._readback_ok = False  # tolerate the FC5/FC6 echo read-back during blackout
        super().__init__(address, values)

    # -- helpers ----------------------------------------------------
    def _hook(self, address, values) -> bool:
        """Run the client-write hook; returns True for client writes only."""
        if self._on_write is None or self.publishing:
            return False
        try:
            self._on_write(self._kind, address, list(values))
        except Exception:  # pragma: no cover - hook bugs must not kill the server
            log.exception("write hook failed")
        return True

    def _check_blackout(self) -> None:
        if self._state.comm_blackout:
            raise ModbusException("sensor bus blackout: reads unavailable")

    # -- server path -----------------------------------------------
    # pymodbus reaches the block through its sync methods (the device context
    # delegates async_* to sync), so blackout semantics live here.
    def getValues(self, address, count=1):
        if self._readback_ok:
            # the value echo that follows a client write must succeed even in
            # blackout, otherwise the fault can never be cleared remotely
            self._readback_ok = False
            return super().getValues(address, count)
        self._check_blackout()
        return super().getValues(address, count)

    def setValues(self, address, values):
        client_write = self._hook(address, values)
        result = super().setValues(address, values)
        if client_write:
            self._readback_ok = True
        return result

    async def async_getValues(self, address, count=1):
        return self.getValues(address, count)

    async def async_setValues(self, address, values):
        client_write = self._hook(address, values)
        result = await super().async_setValues(address, values)
        if client_write:
            self._readback_ok = True
        return result


def _apply_holding(reg, state: SimState, raw: int) -> None:
    name, val = reg.name, decode(reg, int(raw))
    if name == "pump_speed_setpoint_pct":
        state.pump_setpoint_pct = val
    elif name == "valve_position_setpoint_pct":
        state.valve_setpoint_pct = val
    elif name == "it_heat_load_kw":
        state.heat_load_kw = val
    elif name == "slam_valve_command":
        state.slam_command = val > 0
    else:
        log.debug("ignored write to %s (sensor register)", name)


def _apply_coil(reg, state: SimState, value: bool) -> None:
    name = reg.name
    if name.startswith("leak_zone_"):
        idx = "abcd".index(name[-1])
        state.leak_zones[idx] = bool(value)
        log.warning("leak zone %s: %s", name[-1].upper(), "TRIPPED" if value else "cleared")
    elif name == "comm_blackout":
        state.comm_blackout = bool(value)
        log.warning("sensor bus blackout: %s", "engaged" if value else "cleared")
    else:
        log.debug("ignored coil write to %s", name)


def _apply_write(map_: ModbusMap, state: SimState, kind: str, address: int, values) -> None:
    """``address`` is block-level, i.e. wire address + 1.

    Holding and coil addresses share the same wire space (both start at 0),
    so the block kind decides which map applies.
    """
    wire = address - 1
    regs = map_.holding if kind == "holding" else map_.coils
    for reg in regs:
        if reg.addr == wire:
            if kind == "holding":
                _apply_holding(reg, state, values[0])
            else:
                _apply_coil(reg, state, bool(values[0]))
            return
    log.debug("ignored write to %s address %s", kind, wire)


def _publish_pairs(map_: ModbusMap, store, hr_block, co_block, pairs) -> None:
    """Write (name, value) pairs into the datastore, bypassing the hook."""
    hr_block.publishing = co_block.publishing = True
    try:
        for name, value in pairs:
            reg = map_.holding_by_name(name)
            store.setValues(3, reg.addr, [encode(reg, value)])
    finally:
        hr_block.publishing = co_block.publishing = False


def _publish(map_: ModbusMap, store, hr_block, co_block, state: SimState, rng: random.Random) -> None:
    """Mirror the true state into the datastore with sensor noise applied."""
    _publish_pairs(
        map_, store, hr_block, co_block,
        [
            ("secondary_supply_temp_c", state.t_secondary_supply_c + rng.gauss(0, 0.15)),
            ("secondary_return_temp_c", state.t_secondary_return_c + rng.gauss(0, 0.15)),
            ("primary_supply_temp_c", state.t_primary_supply_c + rng.gauss(0, 0.10)),
            ("primary_return_temp_c", state.t_primary_return_c + rng.gauss(0, 0.10)),
            ("secondary_flow_lpm", max(0.0, state.flow_effective_lpm + rng.gauss(0, 0.5))),
            ("secondary_dp_mbar", max(0.0, state.dp_mbar + rng.gauss(0, 3.0))),
            ("pump_speed_actual_pct", state.pump_actual_pct),
            ("valve_position_actual_pct", state.valve_actual_pct),
            ("alarm_word", ph.alarm_word(state)),
            ("status_word", ph.status_word(state)),
        ],
    )


async def run_server(map_: ModbusMap, params: SimParams, host: str, port: int) -> None:
    state = ph.initial_state(params)
    rng = random.Random(params.seed + 1)

    def on_write(kind: str, address: int, values) -> None:
        _apply_write(map_, state, kind, address, values)

    hr_block = CduDataBlock(0, [0] * (map_.holding_count + 1), state=state, kind="holding", on_write=on_write)
    co_block = CduDataBlock(0, [False] * (map_.coil_count + 1), state=state, kind="coils", on_write=on_write)
    store = ModbusDeviceContext(hr=hr_block, co=co_block)
    # single=True: every device id is served by this one context
    context = ModbusServerContext(devices=store, single=True)

    # Prime the datastore with the steady state BEFORE the socket is served,
    # so a freshly connected client never observes a wall of zeros (which
    # downstream hammer guards would misread as a flow transient).
    _publish(map_, store, hr_block, co_block, state, rng)
    _publish_pairs(
        map_, store, hr_block, co_block,
        [
            ("pump_speed_setpoint_pct", state.pump_setpoint_pct),
            ("valve_position_setpoint_pct", state.valve_setpoint_pct),
            ("it_heat_load_kw", state.heat_load_kw),
            ("slam_valve_command", 0),
        ],
    )

    log.info(
        "virtual CDU '%s' starting on modbus tcp %s:%s (device_id=%s)",
        map_.device_id, host, port, map_.device_id,
    )
    server_task = asyncio.create_task(StartAsyncTcpServer(context, address=(host, port)))
    await asyncio.sleep(0.5)  # allow the server task to bind
    log.info(
        "steady state: flow=%.1f L/min  dp=%.0f mbar  T_sec(ret)=%.1f degC  heat=%.0f kW",
        state.flow_lpm, state.dp_mbar, state.t_secondary_return_c, state.heat_load_kw,
    )
    try:
        while True:
            ph.step(state, params, params.dt, rng)
            _publish(map_, store, hr_block, co_block, state, rng)
            await asyncio.sleep(params.dt)
    except asyncio.CancelledError:
        raise
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
