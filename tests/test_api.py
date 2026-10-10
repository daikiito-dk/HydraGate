"""In-process API tests (no simulator required)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from hydragate.bridge.api import BridgeContext, create_app
from hydragate.bridge.config import (
    BridgeConfig,
    CduSpec,
    DetectorSpec,
    LoopSpec,
)
from hydragate.bridge.events import EventBus
from hydragate.bridge.hammer import HammerParams
from hydragate.bridge.metrics import BridgeMetrics
from hydragate.bridge.state import SharedState, Snapshot
from hydragate.mapfile import load_map


def _ctx() -> BridgeContext:
    cfg = BridgeConfig()
    cfg.manager_id = "hydragate-01"
    cfg.cdus = [
        CduSpec(
            id="cdu-01",
            model="VirtualCDU 250",
            serial="S-1",
            loops=[
                LoopSpec(id="loop-secondary-tcs-01", kind="SecondaryTCS"),
                LoopSpec(id="loop-primary-fws-01", kind="PrimaryFWS"),
            ],
            leak_detectors=[
                DetectorSpec(id="leak-a", zone="Rack row A", coil=0),
                DetectorSpec(id="leak-b", zone="Rack row B", coil=1),
            ],
        )
    ]
    return BridgeContext(
        shared=SharedState(),
        events=EventBus(),
        metrics=BridgeMetrics(),
        cfg=cfg,
        map_=load_map(),
        hammer=HammerParams(),
        fsm=None,
    )


def test_service_root_links() -> None:
    client = TestClient(create_app(_ctx()))
    root = client.get("/redfish/v1/")
    assert root.status_code == 200
    links = root.json()["Links"]
    assert links["CoolingUnits"][0]["@odata.id"] == "/redfish/v1/CoolingUnits/cdu-01"
    assert links["EventService"]["@odata.id"] == "/redfish/v1/EventService"
    assert len(links["CoolingLoops"]) == 2


def test_collections_and_detectors() -> None:
    client = TestClient(create_app(_ctx()))
    detectors = client.get("/redfish/v1/LeakDetectors").json()
    assert detectors["Members@odata.count"] == 2
    item = client.get("/redfish/v1/LeakDetectors/leak-a").json()
    assert item["LeakDetected"] is False
    assert item["Status"]["Health"] == "OK"
    assert client.get("/redfish/v1/LeakDetectors/nope").status_code == 404


def test_cooling_unit_without_telemetry_is_critical() -> None:
    client = TestClient(create_app(_ctx()))
    unit = client.get("/redfish/v1/CoolingUnits/cdu-01").json()
    assert unit["Status"]["Health"] == "Critical"
    assert unit["TelemetryStale"] is True
    assert unit["SecondaryLoop"]["FlowRateLPM"] is None


def test_healthz_and_metrics() -> None:
    ctx = _ctx()
    client = TestClient(create_app(ctx))
    assert client.get("/healthz").json()["status"] == "degraded"
    metrics = client.get("/metrics").text
    assert "hydragate_fsm_state" in metrics


def test_fsm_diagnostics_with_fsm_attached() -> None:
    ctx = _ctx()
    ctx.fsm = type("Fsm", (), {"actions": [], "leak_detected_ms": None})()
    client = TestClient(create_app(ctx))
    diag = client.get("/internal/fsm").json()
    assert diag["state"] == "NORMAL"
    assert diag["actions"] == []
    assert diag["hammer"]["max_surge_pressure_pa"] == 5.0e5


def test_snapshot_to_dict() -> None:
    snap = Snapshot(
        ts_ms=123,
        t_secondary_supply_c=21.5,
        t_secondary_return_c=27.7,
        t_primary_supply_c=18.0,
        t_primary_return_c=21.8,
        flow_lpm=200.0,
        dp_mbar=130.0,
        pump_setpoint_pct=100.0,
        pump_actual_pct=100.0,
        valve_setpoint_pct=100.0,
        valve_actual_pct=100.0,
        heat_load_kw=80.0,
        alarm_word=0,
        status_word=11,
        leak_zones=(False, False, False, False),
    )
    d = snap.to_dict()
    assert d["delta_t_k"] == 6.2
    assert d["secondary_flow_lpm"] == 200.0
    assert d["leak_zones"] == [False, False, False, False]
    assert d["ts_ms"] == 123
