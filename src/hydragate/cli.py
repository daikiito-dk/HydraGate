"""HydraGate command-line entry points."""
from __future__ import annotations

import argparse
import asyncio
import logging


def sim_main(argv=None) -> None:
    """Run the virtual CDU Modbus TCP simulator."""
    from .mapfile import load_map
    from .sim.physics import SimParams
    from .sim.server import run_server

    ap = argparse.ArgumentParser(prog="hydragate-sim", description="Virtual CDU Modbus TCP simulator")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=15020)
    ap.add_argument("--unit-id", type=int, default=None, help="override device id from the map")
    ap.add_argument("--map", default=None, help="path to modbus_map.yaml")
    ap.add_argument("--facility-temp", type=float, default=18.0, help="FWS supply temperature (degC)")
    ap.add_argument("--heat-load", type=float, default=80.0, help="initial IT heat load (kW)")
    ap.add_argument("--dt", type=float, default=0.1, help="physics tick (s)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    map_ = load_map(args.map)
    if args.unit_id is not None:
        # device id comes from the map; allow a runtime override for tests
        import dataclasses

        map_ = dataclasses.replace(map_, device_id=args.unit_id)
    params = SimParams(
        facility_supply_c=args.facility_temp,
        initial_heat_load_kw=args.heat_load,
        dt=args.dt,
    )
    try:
        asyncio.run(run_server(map_, params, args.host, args.port))
    except KeyboardInterrupt:
        pass


def bridge_main(argv=None) -> None:
    """Run the Modbus -> Redfish protocol bridge."""
    from .bridge.api import run_bridge

    run_bridge(argv)


def chaos_main(argv=None) -> None:
    """Inject faults into the virtual CDU (leak, heat spike, slam, blackout)."""
    from pymodbus.client import ModbusTcpClient

    from .mapfile import encode, load_map

    ap = argparse.ArgumentParser(prog="hydragate-chaos", description="Fault injector for the virtual CDU")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15020)
    ap.add_argument("--unit-id", type=int, default=None)
    ap.add_argument("--map", default=None)
    sub = ap.add_subparsers(dest="action", required=True)

    p_leak = sub.add_parser("leak", help="trip / clear a leak detector zone")
    p_leak.add_argument("--zone", required=True, choices=["a", "b", "c", "d"])
    p_leak.add_argument("--clear", action="store_true")

    p_heat = sub.add_parser("heat", help="inject an IT heat-load spike")
    p_heat.add_argument("--kw", type=float, default=120.0)

    p_slam = sub.add_parser("slam", help="command an instant valve slam (negative control)")
    p_slam.add_argument("--off", action="store_true", help="clear the slam latch")

    p_blackout = sub.add_parser("blackout", help="engage / clear the sensor-bus blackout")
    p_blackout.add_argument("--off", action="store_true")

    p_pump = sub.add_parser("pump", help="set pump speed setpoint (pct)")
    p_pump.add_argument("--pct", type=float, required=True)

    p_valve = sub.add_parser("valve", help="set valve position setpoint (pct)")
    p_valve.add_argument("--pct", type=float, required=True)

    args = ap.parse_args(argv)
    map_ = load_map(args.map)
    unit = args.unit_id if args.unit_id is not None else map_.device_id

    def write_register(name: str, raw: int) -> None:
        reg = map_.holding_by_name(name)
        rr = client.write_register(reg.addr, int(raw), device_id=unit)
        if rr.isError():
            raise SystemExit(f"modbus write failed for {name}: {rr}")

    def write_coil(name: str, value: bool) -> None:
        reg = map_.coil_by_name(name)
        rr = client.write_coil(reg.addr, bool(value), device_id=unit)
        if rr.isError():
            raise SystemExit(f"modbus write failed for {name}: {rr}")

    client = ModbusTcpClient(args.host, port=args.port, timeout=3)
    if not client.connect():
        raise SystemExit(f"cannot connect to virtual CDU at {args.host}:{args.port}")
    try:
        if args.action == "leak":
            write_coil(f"leak_zone_{args.zone}", not args.clear)
            print(f"leak zone {args.zone.upper()}: {'cleared' if args.clear else 'TRIPPED'}")
        elif args.action == "heat":
            reg = map_.holding_by_name("it_heat_load_kw")
            write_register(reg.name, encode(reg, args.kw))
            print(f"IT heat load set to {args.kw} kW")
        elif args.action == "slam":
            write_register("slam_valve_command", 0 if args.off else 1)
            print("slam command cleared" if args.off else "slam command issued (instant valve close)")
        elif args.action == "blackout":
            write_coil("comm_blackout", not args.off)
            print("sensor-bus blackout cleared" if args.off else "sensor-bus blackout ENGAGED")
        elif args.action == "pump":
            reg = map_.holding_by_name("pump_speed_setpoint_pct")
            write_register(reg.name, encode(reg, args.pct))
            print(f"pump setpoint -> {args.pct}%")
        elif args.action == "valve":
            reg = map_.holding_by_name("valve_position_setpoint_pct")
            write_register(reg.name, encode(reg, args.pct))
            print(f"valve setpoint -> {args.pct}%")
    finally:
        client.close()
