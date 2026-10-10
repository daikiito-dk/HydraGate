"""Unit tests for the virtual CDU thermohydraulic model."""
from __future__ import annotations

import random

import pytest

from hydragate.sim import physics as ph


def _steady() -> tuple[ph.SimParams, ph.SimState]:
    p = ph.SimParams()
    return p, ph.initial_state(p)


def test_steady_state_full_flow() -> None:
    _, s = _steady()
    assert 195.0 <= s.flow_lpm <= 205.0
    assert s.pump_actual_pct == pytest.approx(100.0)
    assert s.valve_actual_pct == pytest.approx(100.0)


def test_heat_balance() -> None:
    _, s = _steady()
    expected_dt = 80.0 * 60.0 / (s.flow_lpm * 1.03 * 3.85)
    actual_dt = s.t_secondary_return_c - s.t_secondary_supply_c
    assert actual_dt == pytest.approx(expected_dt, abs=0.6)


def test_slam_stops_flow_instantly() -> None:
    p, s = _steady()
    rng = random.Random(3)
    s.slam_command = True
    ph.step(s, p, p.dt, rng)
    assert s.flow_lpm == 0.0
    assert s.valve_actual_pct == 0.0
    assert s.valve_setpoint_pct == 0.0


def test_pump_cut_reduces_flow() -> None:
    p, s = _steady()
    rng = random.Random(3)
    s.pump_setpoint_pct = 50.0
    for _ in range(int(6.0 / p.dt)):
        ph.step(s, p, p.dt, rng)
    assert s.pump_actual_pct == pytest.approx(50.0, abs=0.5)
    assert s.flow_lpm == pytest.approx(100.0, abs=8.0)


def test_alarm_word_bits() -> None:
    _, s = _steady()
    s.leak_zones[2] = True
    assert ph.alarm_word(s) & ph.ALARM_LEAK

    s.flow_lpm = 5.0  # low flow with pump and valve still open
    assert ph.alarm_word(s) & ph.ALARM_LOW_FLOW

    s.t_secondary_return_c = 50.0
    assert ph.alarm_word(s) & ph.ALARM_HIGH_RETURN

    s.pump_actual_pct = 0.0
    s.valve_actual_pct = 0.0  # flow stopped intentionally: no low-flow alarm
    word = ph.alarm_word(s)
    assert not (word & ph.ALARM_LOW_FLOW) or (word & ph.ALARM_LEAK)


def test_leak_draws_off_flow() -> None:
    p, s = _steady()
    rng = random.Random(5)
    base_flow = s.flow_lpm
    s.leak_zones = [True, True, False, False]
    for _ in range(int(5.0 / p.dt)):
        ph.step(s, p, p.dt, rng)
    assert s.flow_effective_lpm < base_flow  # leak draw-off reduces delivered flow
    assert s.flow_effective_lpm == pytest.approx(base_flow - 2 * 2.0, abs=0.5)
    assert ph.status_word(s) & ph.ST_COMM_OK
