# HydraGate

**Vendor-neutral liquid-cooling telemetry & safety gateway for data centers.**

HydraGate bridges the protocol gap between facility cooling equipment (CDUs speaking
Modbus TCP) and IT infrastructure management (DMTF Redfish-style REST + SSE +
Prometheus), and adds the piece almost every DCIM stack is missing: a
**water-hammer-aware safe-shutdown interlock** for leak response.

```
            Modbus TCP                    Redfish-style REST / SSE / Metrics
┌─────────────┐        ┌──────────────────────────┐        ┌──────────────┐
│  Virtual /  │ ◄────► │         HydraGate        │ ◄────► │  DCIM, ops,  │
│  real CDU   │ 10 Hz  │  poller · FSM · hammer   │  HTTP  │  dashboards  │
│  (FWS+TCS)  │  poll  │  guard · event bus      │  SSE   │  alerting    │
└─────────────┘        └──────────────────────────┘        └──────────────┘
                          ▲                    │
                leak rope ┘                    ▼
                sensors            staged pump ramp-down + valve closure
                                   (Joukowsky envelope enforced)
```

## Why

Direct-to-chip liquid cooling couples facility plumbing (OT) with IT racks, but the
software world around it is fragmented: CDUs speak Modbus/BACnet, IT speaks Redfish,
and nobody owns the leak-response safety chain. Closing a valve on a high-flow loop
instantly is not a safe shutdown — it is a water hammer:

$$\Delta P = \rho \cdot c \cdot \Delta v \quad \text{(Joukowsky)}$$

At 200 L/min in a DN50 loop, an instant slam implies a **~24 bar** pressure spike —
enough to burst fittings and cold plates, turning a leak into an outage. HydraGate's
interlock instead coordinates pump ramp-down with staged valve closure, keeping the
implied surge inside a configured envelope (default **5 bar**) while still issuing
its first corrective write within ~100 ms of leak detection. A runtime guard audits
measured telemetry and flags *anyone* who violates the envelope — including naive
controllers that are not HydraGate.

## Status

Prototype / MVP. The Redfish interface is **Redfish-inspired** (modeled on the
DMTF `CoolingUnit` / `CoolingLoop` / `LeakDetector` schemas, not yet DSP0158-certified),
and the only tested device is the bundled **virtual CDU simulator**. Do not point
this at real hardware without proper safety engineering review.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/daikiito-dk/HydraGate && cd HydraGate
uv sync                       # create .venv and install

# terminal 1 - virtual CDU (steady state: 200 L/min, 80 kW IT load)
uv run hydragate-sim

# terminal 2 - protocol bridge (Redfish API on :8080)
uv run hydragate-bridge
```

Explore:

```bash
curl -s localhost:8080/redfish/v1/ | jq
curl -s localhost:8080/redfish/v1/CoolingUnits/cdu-01 | jq '.Status, .SecondaryLoop'
curl -s localhost:8080/metrics | grep hydragate_secondary_flow_lpm
curl -sN localhost:8080/redfish/v1/EventService/SSE   # live event stream
```

## Demo: leak response in three terminals

```bash
# terminal 3 - trip the rack-row-B leak rope
uv run hydragate-chaos leak --zone b
```

Watch terminal 2's SSE stream: a `LeakDetected` event lands within ~150 ms, the pump
setpoint is cut immediately (momentum bleed), the isolation valve then closes in
stages sized to the surge budget, and the loop reaches `ISOLATED` in a few seconds:

```
event: LeakDetected        data: {"message":"coolant leak detected: leak-b", ...}
event: InterlockAction     data: {"detail":"pump 100% -> 70% (momentum bleed)", ...}
event: InterlockAction     data: {"detail":"valve 100% -> 55% at flow 147 lpm", ...}
...
event: IsolationComplete   data: {"message":"loop isolated in 7.4 s", ...}
event: MaintenanceRecommended  data: {"message":"de-energize affected IT racks ...", ...}
```

The negative control — what HydraGate exists to prevent:

```bash
uv run hydragate-chaos slam      # instant valve slam: the WRONG shutdown
# -> event: UnsafeClosureDetected  data: {"message":"water-hammer surge 23.59 bar exceeds the 5.00 bar limit", ...}
```

Other fault injection:

```bash
uv run hydragate-chaos heat --kw 320      # thermal excursion -> ThermalWarning, no isolation
uv run hydragate-chaos blackout          # sensor-bus blackout -> CommunicationLost, stale data
uv run hydragate-chaos pump --pct 100     # manual control
uv run hydragate-chaos valve --pct 100
```

Docker alternative (single image, two roles):

```bash
docker compose up --build          # sim + bridge
```

## Architecture

| Component | Path | What it does |
|---|---|---|
| Virtual CDU simulator | `src/hydragate/sim/` | pymodbus TCP server wrapping a compact thermohydraulic model (TCS + FWS loops, pump/system curves, leak draw-off, actuator ramps, sensor noise, fault-injection registers) |
| Modbus map (single source of truth) | `spec/modbus_map.yaml` | wire contract shared by simulator, bridge and chaos injector — the two sides cannot drift |
| Poller | `src/hydragate/bridge/poller.py` | 10 Hz register polling, snapshot ring buffer, staleness detection |
| Safety interlock FSM | `src/hydragate/bridge/fsm.py` | leak latch, staged shutdown with per-stage surge budget, timeout faults, warning monitors |
| Hammer guard | `src/hydragate/bridge/hammer.py` | Joukowsky math + runtime audit of measured flow; flags unsafe closures from *any* source |
| Redfish API | `src/hydragate/bridge/api.py` | CoolingUnit / CoolingLoop / LeakDetector / Manager / EventService, SSE, Prometheus `/metrics` |
| Chaos injector | `hydragate-chaos` | leak, heat spike, slam, blackout, manual pump/valve |

## Validation contracts (spec-driven)

The pass/fail criteria live in `spec/contracts/*.yaml` and are executed by
`tests/test_contracts.py` against the **real** sim + bridge subprocess stack:

| Contract | Asserts |
|---|---|
| `leak_isolation` | SSE `LeakDetected` ≤ 500 ms after trigger; first corrective write ≤ 250 ms after the event; ends `ISOLATED` with valve ≤ 5 %, pump ≤ 10 %; measured Joukowsky surge ≤ 5 bar; no `UnsafeClosureDetected` |
| `communication_loss` | `CommunicationLost` + `COMMS_LOST` + stale telemetry within 6 s; recovery on clear, no spurious shutdown |
| `naive_slam_negative_control` | an instant slam *is* detected as unsafe while the interlock stays calm |
| `heat_spike_warning` | thermal excursion raises `ThermalWarning` without triggering isolation |

```bash
uv run pytest -q        # 20 tests: 4 contracts + physics + hammer math + API units
```

The topology in `config.yaml` is a static stand-in for a future NetBox sync worker
(NetBox v4.7 cooling models: CoolingSource → CoolingFeed → CoolingIntake/Outflow).

## Roadmap

- [ ] NetBox v4.7 cooling-topology sync (auto-register monitored endpoints)
- [ ] BACnet/IP and vendor register-map profiles (Motivair, CoolIT, Vertiv, nVent)
- [ ] Full DMTF Redfish schema conformance (DSP0158) + official mockup tests
- [ ] Rust/Go rewrite of the interlock for hard real-time deployments; policy push to edge PLCs
- [ ] Coolant health analytics (conductivity/pH trending, inhibitor depletion)
- [ ] Per-rack thermal billing: $\dot Q = \rho c_p Q \Delta T$ metering for colocation tenants

## License

Apache-2.0. See [LICENSE](LICENSE).
日本語のREADMEは[README.ja.md](README.ja.md)をご覧ください。
