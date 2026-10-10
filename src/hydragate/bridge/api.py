"""Redfish-style REST API, SSE event stream and Prometheus metrics."""
from __future__ import annotations

import json
import logging
import queue
from dataclasses import dataclass

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from .. import __version__
from ..mapfile import ModbusMap
from .config import BridgeConfig, CduSpec
from .events import EventBus
from .hammer import HammerParams
from .metrics import BridgeMetrics
from .state import SharedState, now_ms

log = logging.getLogger("hydragate.api")

PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@dataclass
class BridgeContext:
    shared: SharedState
    events: EventBus
    metrics: BridgeMetrics
    cfg: BridgeConfig
    map_: ModbusMap
    hammer: HammerParams
    fsm: object


def _mode_for(state: str) -> str:
    return {
        "NORMAL": "Normal",
        "RESPONDING": "SafeShutdown",
        "STAGED_ISOLATION": "SafeShutdown",
        "ISOLATED": "Isolated",
        "COMMS_LOST": "Unavailable",
        "FAULT": "Fault",
    }.get(state, "Normal")


def create_app(ctx: BridgeContext) -> FastAPI:
    app = FastAPI(title="HydraGate", version=__version__)
    cfg = ctx.cfg

    def cdu() -> CduSpec:
        return cfg.cdus[0] if cfg.cdus else CduSpec(id="cdu-01")

    def snap():
        return ctx.shared.latest

    def health(s) -> str:
        if s is None or not ctx.shared.comms_ok:
            return "Critical"
        if any(s.leak_zones) or ctx.shared.fsm_state == "FAULT":
            return "Critical"
        th = cfg.thresholds
        if (
            s.t_secondary_return_c > th.secondary_return_temp_warning_c
            or s.delta_t_k > th.delta_t_warning_k
        ):
            return "Warning"
        return "OK"

    # ------------------------------------------------------------ root
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/redfish/v1/")

    @app.get("/redfish/v1/", summary="Redfish service root")
    def service_root():
        return {
            "@odata.id": "/redfish/v1/",
            "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
            "Id": "RootService",
            "Name": "HydraGate Redfish Service",
            "RedfishVersion": "1.20.0",
            "Vendor": "HydraGate",
            "Version": __version__,
            "Links": {
                "CoolingUnits": [{"@odata.id": f"/redfish/v1/CoolingUnits/{cdu().id}"}],
                "CoolingLoops": [
                    {"@odata.id": f"/redfish/v1/CoolingLoops/{l.id}"} for l in cdu().loops
                ],
                "LeakDetectors": [{"@odata.id": f"/redfish/v1/LeakDetectors"}],
                "Managers": [{"@odata.id": f"/redfish/v1/Managers/{cfg.manager_id}"}],
                "EventService": {"@odata.id": "/redfish/v1/EventService"},
            },
        }

    # ------------------------------------------------------------ CoolingUnit
    @app.get("/redfish/v1/CoolingUnits", summary="CoolingUnit collection")
    def cooling_units():
        return {
            "@odata.id": "/redfish/v1/CoolingUnits",
            "@odata.type": "#CoolingUnitCollection.CoolingUnitCollection",
            "Name": "Cooling Units",
            "Members": [{"@odata.id": f"/redfish/v1/CoolingUnits/{cdu().id}"}],
            "Members@odata.count": 1,
        }

    @app.get("/redfish/v1/CoolingUnits/{cdu_id}", summary="CoolingUnit (CDU)")
    def cooling_unit(cdu_id: str):
        s = snap()
        if cdu_id != cdu().id:
            return Response(status_code=404, content=json.dumps({"error": "unknown CDU"}), media_type="application/json")
        sec = next((l for l in cdu().loops if l.kind == "SecondaryTCS"), None)
        pri = next((l for l in cdu().loops if l.kind == "PrimaryFWS"), None)
        body = {
            "@odata.id": f"/redfish/v1/CoolingUnits/{cdu().id}",
            "@odata.type": "#CoolingUnit.v1_0_0.CoolingUnit",
            "Id": cdu().id,
            "Name": cdu().model or cdu().id,
            "Model": cdu().model,
            "SerialNumber": cdu().serial,
            "Status": {
                "State": "Enabled",
                "Health": health(s),
            },
            "OperatingMode": _mode_for(ctx.shared.fsm_state),
            "InterlockState": ctx.shared.fsm_state,
            "TelemetryStale": (not ctx.shared.comms_ok) or ctx.shared.latest is None,
            "SecondaryLoop": {
                "@odata.id": f"/redfish/v1/CoolingLoops/{sec.id if sec else 'secondary'}",
                "SupplyTemperatureC": None if s is None else round(s.t_secondary_supply_c, 1),
                "ReturnTemperatureC": None if s is None else round(s.t_secondary_return_c, 1),
                "DeltaTK": None if s is None else round(s.delta_t_k, 2),
                "FlowRateLPM": None if s is None else round(s.flow_lpm, 1),
                "DifferentialPressureMbar": None if s is None else round(s.dp_mbar, 1),
                "PumpSpeedSetpointPct": None if s is None else s.pump_setpoint_pct,
                "PumpSpeedPct": None if s is None else s.pump_actual_pct,
                "ValvePositionSetpointPct": None if s is None else s.valve_setpoint_pct,
                "ValvePositionPct": None if s is None else s.valve_actual_pct,
            },
            "PrimaryLoop": {
                "@odata.id": f"/redfish/v1/CoolingLoops/{pri.id if pri else 'primary'}",
                "SupplyTemperatureC": None if s is None else round(s.t_primary_supply_c, 1),
                "ReturnTemperatureC": None if s is None else round(s.t_primary_return_c, 1),
            },
            "HeatLoadKW": None if s is None else round(s.heat_load_kw, 1),
            "LeakDetectors": [
                {"@odata.id": f"/redfish/v1/LeakDetectors/{d.id}"} for d in cdu().leak_detectors
            ],
        }
        return body

    # ------------------------------------------------------------ CoolingLoop
    @app.get("/redfish/v1/CoolingLoops", summary="CoolingLoop collection")
    def cooling_loops():
        return {
            "@odata.id": "/redfish/v1/CoolingLoops",
            "@odata.type": "#CoolingLoopCollection.CoolingLoopCollection",
            "Name": "Cooling Loops",
            "Members": [{"@odata.id": f"/redfish/v1/CoolingLoops/{l.id}"} for l in cdu().loops],
            "Members@odata.count": len(cdu().loops),
        }

    @app.get("/redfish/v1/CoolingLoops/{loop_id}", summary="CoolingLoop")
    def cooling_loop(loop_id: str):
        spec = next((l for l in cdu().loops if l.id == loop_id), None)
        if spec is None:
            return Response(status_code=404, content=json.dumps({"error": "unknown loop"}), media_type="application/json")
        s = snap()
        body = {
            "@odata.id": f"/redfish/v1/CoolingLoops/{loop_id}",
            "@odata.type": "#CoolingLoop.v1_0_0.CoolingLoop",
            "Id": spec.id,
            "Name": spec.id,
            "LoopType": spec.kind,
            "Status": {"State": "Enabled", "Health": health(s)},
        }
        if spec.kind == "SecondaryTCS" and s is not None:
            body.update(
                SupplyTemperatureC=round(s.t_secondary_supply_c, 1),
                ReturnTemperatureC=round(s.t_secondary_return_c, 1),
                DeltaTK=round(s.delta_t_k, 2),
                FlowRateLPM=round(s.flow_lpm, 1),
                DifferentialPressureMbar=round(s.dp_mbar, 1),
            )
        elif spec.kind == "PrimaryFWS" and s is not None:
            body.update(
                SupplyTemperatureC=round(s.t_primary_supply_c, 1),
                ReturnTemperatureC=round(s.t_primary_return_c, 1),
            )
        return body

    # ------------------------------------------------------------ LeakDetector
    def detector_states() -> list[dict]:
        s = snap()
        dets = sorted(cdu().leak_detectors, key=lambda d: d.coil)
        out = []
        for i, d in enumerate(dets):
            leaking = bool(s is not None and i < len(s.leak_zones) and s.leak_zones[i])
            out.append(
                {
                    "@odata.id": f"/redfish/v1/LeakDetectors/{d.id}",
                    "@odata.type": "#LeakDetector.v1_0_0.LeakDetector",
                    "Id": d.id,
                    "Name": d.zone,
                    "Location": d.zone,
                    "LeakDetected": leaking,
                    "Status": {"State": "Enabled", "Health": "Critical" if leaking else "OK"},
                }
            )
        return out

    @app.get("/redfish/v1/LeakDetectors", summary="LeakDetector collection")
    def leak_detectors():
        members = detector_states()
        return {
            "@odata.id": "/redfish/v1/LeakDetectors",
            "@odata.type": "#LeakDetectorCollection.LeakDetectorCollection",
            "Name": "Leak Detectors",
            "Members": [{"@odata.id": m["@odata.id"]} for m in members],
            "Members@odata.count": len(members),
        }

    @app.get("/redfish/v1/LeakDetectors/{detector_id}", summary="LeakDetector")
    def leak_detector(detector_id: str):
        for m in detector_states():
            if m["Id"] == detector_id:
                return m
        return Response(status_code=404, content=json.dumps({"error": "unknown detector"}), media_type="application/json")

    # ------------------------------------------------------------ Manager
    @app.get("/redfish/v1/Managers", summary="Manager collection")
    def managers():
        return {
            "@odata.id": "/redfish/v1/Managers",
            "@odata.type": "#ManagerCollection.ManagerCollection",
            "Name": "Managers",
            "Members": [{"@odata.id": f"/redfish/v1/Managers/{cfg.manager_id}"}],
            "Members@odata.count": 1,
        }

    @app.get("/redfish/v1/Managers/{manager_id}", summary="Manager (the gateway itself)")
    def manager(manager_id: str):
        if manager_id != cfg.manager_id:
            return Response(status_code=404, content=json.dumps({"error": "unknown manager"}), media_type="application/json")
        return {
            "@odata.id": f"/redfish/v1/Managers/{cfg.manager_id}",
            "@odata.type": "#Manager.v1_20_0.Manager",
            "Id": cfg.manager_id,
            "Name": "HydraGate Gateway",
            "ManagerType": "Service",
            "FirmwareVersion": __version__,
            "UUID": cfg.manager_id,
            "Status": {"State": "Enabled", "Health": "OK" if ctx.shared.comms_ok else "Critical"},
            "Links": {
                "ManagedBy": [{"@odata.id": f"/redfish/v1/CoolingUnits/{cdu().id}"}],
                "Oem": {"HydraGate": {"InterlockState": ctx.shared.fsm_state}},
            },
        }

    # ------------------------------------------------------------ EventService / SSE
    @app.get("/redfish/v1/EventService", summary="Event service")
    def event_service():
        return {
            "@odata.id": "/redfish/v1/EventService",
            "@odata.type": "#EventService.v1_11_0.EventService",
            "Id": "EventService",
            "Name": "Event Service",
            "ServiceEnabled": True,
            "EventFormatType": "Event",
            "SSEUri": "/redfish/v1/EventService/SSE",
            "Subscribe": "open an SSE stream; telemetry heartbeats every 1 s",
        }

    @app.get("/redfish/v1/EventService/SSE", summary="Server-Sent Events stream")
    def sse(request: Request):
        q = ctx.events.subscribe()

        def gen():
            try:
                s = snap()
                if s is not None:
                    yield f"event: MetricReport\ndata: {json.dumps(s.to_dict(), separators=(',', ':'))}\n\n"
                while True:
                    try:
                        ev = q.get(timeout=1.0)
                        yield EventBus.sse_format(ev)
                    except queue.Empty:
                        s = snap()
                        if s is not None:
                            yield f"event: MetricReport\ndata: {json.dumps(s.to_dict(), separators=(',', ':'))}\n\n"
            finally:
                ctx.events.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------ diagnostics
    @app.get("/internal/history", summary="Recent telemetry samples (contract tests)")
    def history(seconds: float = Query(default=10.0, ge=0.1, le=120.0)):
        return {"samples": [s.to_dict() for s in ctx.shared.history_last(seconds)]}

    @app.get("/internal/fsm", summary="Safety FSM status and action log")
    def fsm_status():
        import dataclasses as dc

        return {
            "state": ctx.shared.fsm_state,
            "entered_ms": ctx.shared.fsm_entered_ms,
            "in_state_s": round((now_ms() - ctx.shared.fsm_entered_ms) / 1000.0, 2),
            "leak_detected_ms": getattr(ctx.fsm, "leak_detected_ms", None),
            "actions": [dc.asdict(a) for a in getattr(ctx.fsm, "actions", [])],
            "hammer": {
                "rho_kg_m3": ctx.hammer.rho_kg_m3,
                "wave_speed_m_s": ctx.hammer.wave_speed_m_s,
                "pipe_inner_diameter_mm": ctx.hammer.pipe_inner_diameter_mm,
                "max_surge_pressure_pa": ctx.hammer.max_surge_pressure_pa,
                "window_ms": ctx.hammer.window_ms,
                "max_flow_step_lpm": round(ctx.hammer.max_flow_step_lpm, 2),
            },
        }

    @app.get("/internal/events", summary="Recent events (audit trail)")
    def recent_events(n: int = Query(default=100, ge=1, le=500)):
        return {"events": [e.to_dict() for e in ctx.events.recent(n)]}

    @app.get("/metrics", include_in_schema=False)
    def prometheus():
        return Response(content=ctx.metrics.expose(), media_type=PROM_CONTENT_TYPE)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {
            "status": "ok" if (ctx.shared.comms_ok and ctx.shared.latest is not None) else "degraded",
            "interlock": ctx.shared.fsm_state,
            "telemetry_age_s": None
            if snap() is None
            else round((now_ms() - snap().ts_ms) / 1000.0, 1),
        }

    return app


def run_bridge(argv=None) -> None:
    """Entry point for the hydragate-bridge command."""
    import argparse

    import uvicorn

    from ..mapfile import load_map
    from .config import CduSpec, load_config
    from .fsm import InterlockFSM
    from .poller import ModbusLink, Poller

    ap = argparse.ArgumentParser(prog="hydragate-bridge", description="Modbus -> Redfish protocol bridge")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--map", default=None, help="path to modbus_map.yaml")
    ap.add_argument("--modbus-host", default=None)
    ap.add_argument("--modbus-port", type=int, default=None)
    ap.add_argument("--http-host", default=None)
    ap.add_argument("--http-port", type=int, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    cfg = load_config(args.config)
    map_ = load_map(args.map)
    if args.modbus_host:
        cfg.modbus.host = args.modbus_host
    if args.modbus_port:
        cfg.modbus.port = args.modbus_port
    if args.http_host:
        cfg.http.host = args.http_host
    if args.http_port:
        cfg.http.port = args.http_port

    shared = SharedState()
    metrics = BridgeMetrics()
    events = EventBus(on_publish=lambda ev: metrics.inc_event(ev.message_id))
    link = ModbusLink(map_, cfg.modbus.host, cfg.modbus.port, cfg.modbus.unit_id)
    cdu = cfg.cdus[0] if cfg.cdus else CduSpec(id="cdu-01")
    ctrl = cfg.safety.control
    hammer = HammerParams(
        rho_kg_m3=cfg.safety.rho_kg_m3,
        wave_speed_m_s=cfg.safety.wave_speed_m_s,
        pipe_inner_diameter_mm=cfg.safety.pipe_inner_diameter_mm,
        max_surge_pressure_pa=cfg.safety.max_surge_pressure_pa,
        window_ms=ctrl.valve_step_settle_ms,
    )

    poller = Poller(
        link, shared, events, metrics,
        cfg.modbus.poll_interval_ms, cfg.modbus.stale_after_failures,
        cdu_id=cdu.id, manager_id=cfg.manager_id,
    )
    poller.set_zone_names([d.id for d in sorted(cdu.leak_detectors, key=lambda d: d.coil)])
    fsm = InterlockFSM(shared, link, events, metrics, cfg, hammer, cdu)
    ctx = BridgeContext(
        shared=shared, events=events, metrics=metrics, cfg=cfg, map_=map_, hammer=hammer, fsm=fsm
    )
    app = create_app(ctx)

    poller.start()
    fsm.start()
    log.info(
        "bridge listening on http://%s:%s (redfish /redfish/v1/, sse /redfish/v1/EventService/SSE, metrics /metrics)",
        cfg.http.host, cfg.http.port,
    )
    uvicorn.run(app, host=cfg.http.host, port=cfg.http.port, log_level="warning")
