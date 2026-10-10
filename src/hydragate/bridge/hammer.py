"""Joukowsky water-hammer constraint:  dP = rho * c * dv.

The safety interlock must reduce loop momentum without violating the
allowable surge pressure. This module centralizes the physics of that
constraint so both the FSM (planning) and the guard (auditing) share
identical numbers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .state import Snapshot


@dataclass(frozen=True)
class HammerParams:
    rho_kg_m3: float = 1030.0
    wave_speed_m_s: float = 1350.0
    pipe_inner_diameter_mm: float = 50.0
    max_surge_pressure_pa: float = 5.0e5
    window_ms: int = 600

    @property
    def pipe_area_m2(self) -> float:
        r = self.pipe_inner_diameter_mm / 2000.0
        return math.pi * r * r

    @property
    def max_dv_m_s(self) -> float:
        """Maximum velocity change allowed within the settle window."""
        return self.max_surge_pressure_pa / (self.rho_kg_m3 * self.wave_speed_m_s)

    @property
    def max_flow_step_lpm(self) -> float:
        """Max flow reduction allowed within the settle window (L/min)."""
        return self.max_dv_m_s * self.pipe_area_m2 * 60000.0

    def velocity_m_s(self, flow_lpm: float) -> float:
        return flow_lpm / 60000.0 / self.pipe_area_m2

    def surge_pa(self, flow_before_lpm: float, flow_after_lpm: float) -> float:
        """Joukowsky surge magnitude implied by a flow change."""
        return self.rho_kg_m3 * self.wave_speed_m_s * abs(
            self.velocity_m_s(flow_after_lpm) - self.velocity_m_s(flow_before_lpm)
        )


def window_flow_change(history: list[Snapshot], now: Snapshot, window_ms: int) -> tuple[float, float] | None:
    """Return (flow_now, flow_window_ago) using the sample closest to the
    window edge. Returns None when history does not reach back far enough."""
    target = now.ts_ms - window_ms
    best: Snapshot | None = None
    for s in history:
        if s.ts_ms <= target:
            best = s
        else:
            break
    if best is None or best.ts_ms == now.ts_ms:
        return None
    return now.flow_lpm, best.flow_lpm
