"""Unit tests for the Joukowsky water-hammer constraint math."""
from __future__ import annotations

import pytest

from hydragate.bridge.hammer import HammerParams


def _params() -> HammerParams:
    return HammerParams(
        rho_kg_m3=1030.0,
        wave_speed_m_s=1350.0,
        pipe_inner_diameter_mm=50.0,
        max_surge_pressure_pa=5.0e5,
        window_ms=600,
    )


def test_constraint_math() -> None:
    h = _params()
    assert h.max_dv_m_s == pytest.approx(5.0e5 / (1030.0 * 1350.0), rel=1e-9)
    assert h.max_flow_step_lpm == pytest.approx(h.max_dv_m_s * h.pipe_area_m2 * 60000.0, rel=1e-9)
    # DN50, PG25: the envelope allows roughly 42 L/min per settle window
    assert 40.0 < h.max_flow_step_lpm < 45.0


def test_naive_slam_violates_the_envelope() -> None:
    h = _params()
    surge = h.surge_pa(200.0, 0.0)  # full flow slammed shut instantly
    assert surge > h.max_surge_pressure_pa
    assert surge > 2.0e6  # ~23 bar, pipe-burst territory


def test_staged_step_stays_inside_the_envelope() -> None:
    h = _params()
    staged = h.surge_pa(140.0, 140.0 - 0.9 * h.max_flow_step_lpm)
    assert staged < h.max_surge_pressure_pa


def test_velocity_scales_with_flow() -> None:
    h = _params()
    assert h.velocity_m_s(120.0) == pytest.approx(120.0 / 60000.0 / h.pipe_area_m2, rel=1e-9)
