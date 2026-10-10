"""Shared fixtures: a live sim + bridge subprocess stack per test."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Stack:
    """A virtual CDU simulator plus a protocol bridge, as real processes."""

    def __init__(self, tmp: Path) -> None:
        self.sim_port = free_port()
        self.http_port = free_port()
        self._mb = None
        self._sim_log_path = tmp / "sim.log"
        self._bridge_log_path = tmp / "bridge.log"
        self.sim_log = open(self._sim_log_path, "w")  # noqa: SIM115
        self.bridge_log = open(self._bridge_log_path, "w")  # noqa: SIM115

        self.sim = subprocess.Popen(
            [PY, "-m", "hydragate", "sim", "--port", str(self.sim_port)],
            cwd=REPO, stdout=self.sim_log, stderr=subprocess.STDOUT,
        )
        self._wait_modbus()
        self.bridge = subprocess.Popen(
            [
                PY, "-m", "hydragate", "bridge",
                "--modbus-port", str(self.sim_port),
                "--http-port", str(self.http_port),
            ],
            cwd=REPO, stdout=self.bridge_log, stderr=subprocess.STDOUT,
        )
        self._wait_http()

    # ------------------------------------------------------------ lifecycle
    def _wait_modbus(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.sim_port), timeout=0.5):
                    return
            except OSError:
                if self.sim.poll() is not None:
                    break
                time.sleep(0.2)
        raise RuntimeError(f"simulator did not start:\n{self._tail(self._sim_log_path)}")

    def _wait_http(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{self.http_port}/healthz", timeout=1.0).status_code == 200:
                    return
            except Exception:
                pass
            if self.bridge.poll() is not None:
                break
            time.sleep(0.25)
        raise RuntimeError(f"bridge did not start:\n{self._tail(self._bridge_log_path)}")

    @staticmethod
    def _tail(path: Path, limit: int = 4000) -> str:
        try:
            return path.read_text()[-limit:]
        except Exception:
            return "(log unavailable)"

    def stop(self) -> None:
        for proc in (getattr(self, "bridge", None), getattr(self, "sim", None)):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for handle in (getattr(self, "sim_log", None), getattr(self, "bridge_log", None)):
            if handle and not handle.closed:
                handle.close()
        if self._mb is not None:
            try:
                self._mb.close()
            except Exception:
                pass
            self._mb = None

    # ------------------------------------------------------------ helpers
    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def get(self, path: str):
        return httpx.get(self.base + path, timeout=10.0)

    def chaos(self, action: str, args: dict | None = None) -> float:
        """Run the hydragate-chaos CLI; returns completion time."""
        cmd = [PY, "-m", "hydragate", "chaos", "--port", str(self.sim_port), action]
        if args:
            for key, value in args.items():
                if isinstance(value, bool):
                    if value:
                        cmd.append("--off" if key == "off" else f"--{key.replace('_', '-')}")
                    continue
                cmd.extend([f"--{key.replace('_', '-')}", str(value)])
        result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"chaos failed: {result.stdout} {result.stderr}")
        return time.time()

    def inject(self, action: str, args: dict | None = None) -> float:
        """Direct Modbus fault injection with precise timing.

        Returns the trigger time (immediately after the write lands).
        """
        from pymodbus.client import ModbusTcpClient

        from hydragate.mapfile import encode, load_map

        args = args or {}
        if self._mb is None:
            self._mb = ModbusTcpClient("127.0.0.1", port=self.sim_port, timeout=3)
            self._mb.connect()

        map_ = load_map()

        def wreg(name: str, value: float) -> None:
            reg = map_.holding_by_name(name)
            rr = self._mb.write_register(reg.addr, encode(reg, value), device_id=1)
            assert not rr.isError(), f"write {name} failed"

        def wcoil(name: str, value: bool) -> None:
            reg = map_.coil_by_name(name)
            rr = self._mb.write_coil(reg.addr, bool(value), device_id=1)
            assert not rr.isError(), f"write {name} failed"

        if action == "leak":
            wcoil(f"leak_zone_{args.get('zone', 'a')}", not bool(args.get("clear", False)))
        elif action == "heat":
            wreg("it_heat_load_kw", float(args.get("kw", 120)))
        elif action == "slam":
            wreg("slam_valve_command", 0 if args.get("clear") else 1)
        elif action == "blackout":
            wcoil("comm_blackout", not bool(args.get("clear")))
        elif action == "pump":
            wreg("pump_speed_setpoint_pct", float(args["pct"]))
        elif action == "valve":
            wreg("valve_position_setpoint_pct", float(args["pct"]))
        else:
            raise ValueError(f"unknown fault action: {action}")
        return time.time()


@pytest.fixture()
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.stop()
