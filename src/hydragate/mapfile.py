"""Loader for spec/modbus_map.yaml - the single source of truth for the
Modbus wire contract shared by the simulator, the bridge and the chaos
injector.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_MAP_PATH = Path(__file__).resolve().parents[2] / "spec" / "modbus_map.yaml"


def _map_path(path: str | Path | None) -> Path:
    """Resolve the map file: explicit path > env > repo checkout > cwd."""
    if path:
        return Path(path)
    env = os.environ.get("HYDRAGATE_MAP")
    if env:
        return Path(env)
    if DEFAULT_MAP_PATH.exists():
        return DEFAULT_MAP_PATH
    return Path.cwd() / "spec" / "modbus_map.yaml"


@dataclass(frozen=True)
class Register:
    addr: int
    name: str
    type: str = "u16"          # u16 | i16
    scale: float = 1.0
    unit: str = ""
    role: str = "sensor"       # sensor | control | fault_injection
    rw: bool = False
    desc: str = ""


@dataclass(frozen=True)
class ModbusMap:
    device_id: int
    poll_interval_ms: int
    holding: list[Register]     # ordered by address
    coils: list[Register]
    alarm_bits: dict[int, str]
    status_bits: dict[int, str]

    @property
    def holding_count(self) -> int:
        return max(r.addr for r in self.holding) + 1

    @property
    def coil_count(self) -> int:
        return max(r.addr for r in self.coils) + 1

    def holding_by_name(self, name: str) -> Register:
        for r in self.holding:
            if r.name == name:
                return r
        raise KeyError(name)

    def coil_by_name(self, name: str) -> Register:
        for r in self.coils:
            if r.name == name:
                return r
        raise KeyError(name)


def load_map(path: str | Path | None = None) -> ModbusMap:
    p = _map_path(path)
    data = yaml.safe_load(p.read_text())

    holding = [
        Register(
            addr=int(e["addr"]),
            name=e["name"],
            type=e.get("type", "u16"),
            scale=float(e.get("scale", 1.0)),
            unit=e.get("unit", ""),
            role=e.get("role", "sensor"),
            rw=bool(e.get("rw", False)),
            desc=e.get("desc", ""),
        )
        for e in data["holding_registers"]
    ]
    coils = [
        Register(
            addr=int(e["addr"]),
            name=e["name"],
            role=e.get("role", "sensor"),
            rw=bool(e.get("rw", False)),
            desc=e.get("desc", ""),
        )
        for e in data["coils"]
    ]
    return ModbusMap(
        device_id=int(data["device"]["unit_id"]),
        poll_interval_ms=int(data["polling"]["default_interval_ms"]),
        holding=sorted(holding, key=lambda r: r.addr),
        coils=sorted(coils, key=lambda r: r.addr),
        alarm_bits={int(k): v for k, v in data.get("alarm_bits", {}).items()},
        status_bits={int(k): v for k, v in data.get("status_bits", {}).items()},
    )


def encode(reg: Register, value: float) -> int:
    """Scale an engineering value to a raw 16-bit register value."""
    raw = int(round(value / reg.scale))
    if reg.type == "i16":
        return max(-32768, min(32767, raw))
    return max(0, min(65535, raw))


def decode(reg: Register, raw: int) -> float:
    """Convert a raw register value back to an engineering value."""
    if reg.type == "i16" and raw >= 32768:
        raw -= 65536
    return raw * reg.scale
