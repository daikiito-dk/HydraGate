"""Runtime configuration for the bridge, loaded from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config.yaml"


def _config_path(path: str | Path | None) -> Path:
    """Resolve config.yaml: explicit path > env > repo checkout > cwd."""
    if path:
        return Path(path)
    env = os.environ.get("HYDRAGATE_CONFIG")
    if env:
        return Path(env)
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return Path.cwd() / "config.yaml"


@dataclass
class ModbusConfig:
    host: str = "127.0.0.1"
    port: int = 15020
    unit_id: int = 1
    poll_interval_ms: int = 100
    stale_after_failures: int = 5


@dataclass
class HttpConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class LoopSpec:
    id: str
    kind: str  # SecondaryTCS | PrimaryFWS


@dataclass
class DetectorSpec:
    id: str
    zone: str
    coil: int


@dataclass
class CduSpec:
    id: str
    model: str = ""
    serial: str = ""
    loops: list[LoopSpec] = field(default_factory=list)
    leak_detectors: list[DetectorSpec] = field(default_factory=list)


@dataclass
class SafetyControl:
    pump_cut_pct: float = 30.0
    pump_cap_pct: float = 70.0
    valve_step_settle_ms: int = 600
    min_valve_step_pct: float = 5.0
    isolation_timeout_s: float = 30.0


@dataclass
class SafetyConfig:
    rho_kg_m3: float = 1030.0
    wave_speed_m_s: float = 1350.0
    pipe_inner_diameter_mm: float = 50.0
    max_surge_pressure_pa: float = 5.0e5
    control: SafetyControl = field(default_factory=SafetyControl)


@dataclass
class Thresholds:
    secondary_return_temp_warning_c: float = 45.0
    delta_t_warning_k: float = 15.0
    min_flow_lpm_warning: float = 20.0


@dataclass
class BridgeConfig:
    manager_id: str = "hydragate-01"
    modbus: ModbusConfig = field(default_factory=ModbusConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    cdus: list[CduSpec] = field(default_factory=list)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)


def load_config(path: str | Path | None = None) -> BridgeConfig:
    p = _config_path(path)
    if not p.exists():
        return BridgeConfig()
    data = yaml.safe_load(p.read_text()) or {}

    cfg = BridgeConfig()
    cfg.manager_id = data.get("topology", {}).get("manager_id", cfg.manager_id)

    mb = data.get("modbus", {})
    cfg.modbus = ModbusConfig(
        host=mb.get("host", cfg.modbus.host),
        port=int(mb.get("port", cfg.modbus.port)),
        unit_id=int(mb.get("unit_id", cfg.modbus.unit_id)),
        poll_interval_ms=int(mb.get("poll_interval_ms", cfg.modbus.poll_interval_ms)),
        stale_after_failures=int(mb.get("stale_after_failures", cfg.modbus.stale_after_failures)),
    )
    hp = data.get("http", {})
    cfg.http = HttpConfig(host=hp.get("host", cfg.http.host), port=int(hp.get("port", cfg.http.port)))

    cdus = []
    for c in data.get("topology", {}).get("cdus", []):
        cdus.append(
            CduSpec(
                id=c["id"],
                model=c.get("model", ""),
                serial=c.get("serial", ""),
                loops=[LoopSpec(id=l["id"], kind=l.get("kind", "SecondaryTCS")) for l in c.get("loops", [])],
                leak_detectors=[
                    DetectorSpec(id=d["id"], zone=d.get("zone", ""), coil=int(d["coil"]))
                    for d in c.get("leak_detectors", [])
                ],
            )
        )
    cfg.cdus = cdus

    sf = data.get("safety", {})
    ctrl = sf.get("control", {})
    cfg.safety = SafetyConfig(
        rho_kg_m3=float(sf.get("coolant", {}).get("density_kg_m3", 1030.0)),
        wave_speed_m_s=float(sf.get("coolant", {}).get("wave_speed_m_s", 1350.0)),
        pipe_inner_diameter_mm=float(sf.get("pipe", {}).get("inner_diameter_mm", 50.0)),
        max_surge_pressure_pa=float(sf.get("max_surge_pressure_pa", 5.0e5)),
        control=SafetyControl(
            pump_cut_pct=float(ctrl.get("pump_cut_pct", 30.0)),
            pump_cap_pct=float(ctrl.get("pump_cap_pct", 70.0)),
            valve_step_settle_ms=int(ctrl.get("valve_step_settle_ms", 600)),
            min_valve_step_pct=float(ctrl.get("min_valve_step_pct", 5.0)),
            isolation_timeout_s=float(ctrl.get("isolation_timeout_s", 30.0)),
        ),
    )

    th = data.get("thresholds", {})
    cfg.thresholds = Thresholds(
        secondary_return_temp_warning_c=float(th.get("secondary_return_temp_warning_c", 45.0)),
        delta_t_warning_k=float(th.get("delta_t_warning_k", 15.0)),
        min_flow_lpm_warning=float(th.get("min_flow_lpm_warning", 20.0)),
    )
    return cfg
