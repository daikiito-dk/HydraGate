"""Virtual CDU physics model.

A deliberately compact thermohydraulic model of a 250 kW-class coolant
distribution unit (CDU): a secondary technology cooling system (TCS)
loop feeding direct-to-chip cold plates, exchanging heat against a
primary facility water system (FWS) loop.

The model is intentionally simple - first-order lags, static pump and
system curves, small sensor noise - because its job is to exercise the
protocol bridge, the safety-interlock FSM and the water-hammer guard,
not to replace CFD.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# --- physical constants -------------------------------------------------
RHO_KG_PER_L = 1.03      # PG25 coolant density
CP_KJ_KGK = 3.85         # PG25 specific heat
RHO_W_KG_PER_L = 1.0     # facility water
CP_W_KJ_KGK = 4.18
F_MAX_LPM = 200.0        # secondary flow at 100% pump / 100% valve
DP_MAX_MBAR = 350.0      # secondary diff pressure at 100% pump, open valve
DP_LOSS_MBAR = 220.0     # system-curve loss coefficient at F_MAX
F_PRIM_LPM = 300.0       # primary side flow (constant facility pump)
PUMP_RAMP_PCT_S = 25.0   # VFD ramp limit
VALVE_RAMP_PCT_S = 60.0  # valve actuator travel limit
FLOW_TAU_S = 0.5         # flow momentum / transport lag
SUPPLY_TAU_S = 3.0       # heat-exchanger approach lag
MAX_DELTA_T_K = 30.0     # cap on IT-side temperature rise (dry-out guard)
LOW_FLOW_LPM = 20.0
HIGH_RETURN_C = 45.0

ALARM_LEAK = 1 << 0
ALARM_PUMP_FAULT = 1 << 1
ALARM_VALVE_FAULT = 1 << 2
ALARM_LOW_FLOW = 1 << 3
ALARM_HIGH_RETURN = 1 << 4
ALARM_STALE = 1 << 5

ST_PUMP_RUNNING = 1 << 0
ST_VALVE_OPEN = 1 << 1
ST_INTERLOCK = 1 << 2
ST_COMM_OK = 1 << 3


@dataclass
class SimParams:
    facility_supply_c: float = 18.0
    initial_heat_load_kw: float = 80.0
    initial_pump_pct: float = 100.0
    initial_valve_pct: float = 100.0
    leak_flow_lpm_per_zone: float = 2.0
    dt: float = 0.1
    seed: int = 7


@dataclass
class SimState:
    t: float = 0.0
    pump_setpoint_pct: float = 100.0
    pump_actual_pct: float = 100.0
    valve_setpoint_pct: float = 100.0
    valve_actual_pct: float = 100.0
    flow_lpm: float = 0.0
    flow_effective_lpm: float = 0.0  # loop flow minus leak draw-off
    dp_mbar: float = 0.0
    t_secondary_supply_c: float = 18.0
    t_secondary_return_c: float = 25.0
    t_primary_supply_c: float = 18.0
    t_primary_return_c: float = 22.0
    heat_load_kw: float = 80.0
    leak_zones: list[bool] = field(default_factory=lambda: [False] * 4)
    slam_command: bool = False
    comm_blackout: bool = False


def initial_state(p: SimParams) -> SimState:
    s = SimState(
        pump_setpoint_pct=p.initial_pump_pct,
        pump_actual_pct=p.initial_pump_pct,
        valve_setpoint_pct=p.initial_valve_pct,
        valve_actual_pct=p.initial_valve_pct,
        heat_load_kw=p.initial_heat_load_kw,
        t_primary_supply_c=p.facility_supply_c,
        t_secondary_supply_c=p.facility_supply_c + 3.0,
    )
    rng = random.Random(p.seed)
    # pre-roll ~10 s so the loop starts at steady state
    for _ in range(int(10.0 / p.dt)):
        step(s, p, p.dt, rng)
    return s


def _ramp(current: float, target: float, rate: float, dt: float) -> float:
    delta = target - current
    return current + max(-rate * dt, min(rate * dt, delta))


def step(s: SimState, p: SimParams, dt: float, rng: random.Random) -> None:
    """Advance the model by dt seconds."""
    s.t += dt

    if s.slam_command:
        # Negative control: instant valve slam -> water hammer by design.
        s.valve_setpoint_pct = 0.0
        s.valve_actual_pct = 0.0
        s.flow_lpm = 0.0
        s.dp_mbar = DP_MAX_MBAR * (s.pump_actual_pct / 100.0) ** 2
    else:
        s.pump_actual_pct = _ramp(s.pump_actual_pct, s.pump_setpoint_pct, PUMP_RAMP_PCT_S, dt)
        s.valve_actual_pct = _ramp(s.valve_actual_pct, s.valve_setpoint_pct, VALVE_RAMP_PCT_S, dt)
        valve_frac = max(0.0, s.valve_actual_pct / 100.0)
        target = F_MAX_LPM * (s.pump_actual_pct / 100.0) * math.sqrt(valve_frac)
        s.flow_lpm += (target - s.flow_lpm) * (1.0 - math.exp(-dt / FLOW_TAU_S))
        # static pump curve minus system-curve losses (deadhead-friendly)
        s.dp_mbar = max(
            0.0,
            DP_MAX_MBAR * (s.pump_actual_pct / 100.0) ** 2
            - DP_LOSS_MBAR * (s.flow_lpm / F_MAX_LPM) ** 2,
        )

    leak_flow = p.leak_flow_lpm_per_zone * sum(s.leak_zones)
    flow_eff = max(0.0, s.flow_lpm - leak_flow)
    s.flow_effective_lpm = flow_eff

    # --- thermal model -------------------------------------------------
    if flow_eff > 1.0:
        delta_t = min(MAX_DELTA_T_K, s.heat_load_kw * 60.0 / (flow_eff * RHO_KG_PER_L * CP_KJ_KGK))
    else:
        delta_t = MAX_DELTA_T_K  # dry loop: return tracks supply + cap

    supply_target = s.t_primary_supply_c + 2.0 + 0.02 * s.heat_load_kw
    s.t_secondary_supply_c += (supply_target - s.t_secondary_supply_c) * (
        1.0 - math.exp(-dt / SUPPLY_TAU_S)
    )
    s.t_secondary_return_c = s.t_secondary_supply_c + delta_t
    q_kw = flow_eff * RHO_KG_PER_L * CP_KJ_KGK * (s.t_secondary_return_c - s.t_secondary_supply_c) / 60.0
    s.t_primary_return_c = s.t_primary_supply_c + q_kw * 60.0 / (F_PRIM_LPM * RHO_W_KG_PER_L * CP_W_KJ_KGK)


def alarm_word(s: SimState) -> int:
    flow_eff = s.flow_lpm - 2.0 * sum(s.leak_zones)
    word = 0
    if any(s.leak_zones):
        word |= ALARM_LEAK
    if flow_eff < LOW_FLOW_LPM and s.pump_actual_pct > 10.0 and s.valve_actual_pct > 10.0:
        word |= ALARM_LOW_FLOW
    if s.t_secondary_return_c > HIGH_RETURN_C:
        word |= ALARM_HIGH_RETURN
    return word


def status_word(s: SimState) -> int:
    word = 0
    if s.pump_actual_pct > 1.0:
        word |= ST_PUMP_RUNNING
    if s.valve_actual_pct > 1.0:
        word |= ST_VALVE_OPEN
    if not s.comm_blackout:
        word |= ST_COMM_OK
    return word
