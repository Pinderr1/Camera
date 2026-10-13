# Sentinel: development guide

This is the authoritative guide for Claude Code and Codex sessions in this repo. `AGENTS.md` points here. If code and this file disagree, fix one of them in the same change.

## What Sentinel is

Sentinel is a local-first, multi-camera security system for homes and small businesses. Its job is **continuous situational awareness, not isolated motion alerts**. It tracks people and vehicles over time and across cameras, reasons about sequences of behaviour inside configured property zones, groups related observations into incidents, and notifies a person only when an incident is worth their attention.

A drone will later act as Sentinel's **mobile camera, not its brain**. Sentinel proposes an inspection, a human operator approves it, and a certified flight stack (ArduPilot/PX4) flies it.

Long-term goal: a commercially viable platform for residential properties and small businesses. Every licensing, privacy, and security decision should hold up for a product that is sold.

History: the repo started as CameraAI, a senior night-safety monitor (`src/senior_safety/` plus the BedWatch phone app in `webapp/`). That direction ended in October 2026. Its documents live in `docs/history/senior-night-safety/`. Its code is frozen but kept, because Sentinel reuses parts of it.

## Current status (October 2026)

| Area | Status |
| --- | --- |
| Multi-camera ingestion (RTSP/HTTP/USB/file/simulated) | Built. RTSP verified against a local MediaMTX server. No real IP camera tested yet |
| Real-footage benchmark (`sentinel.bench`) | Built: recorded and live feeds, accuracy against annotations, latency, CPU/memory, multi-camera sessions, playback viewer, reports |
| Object detection (MediaPipe EfficientDet-Lite, CPU, optional tiling) | Built. **Benchmarked on real footage: high precision, low recall** (see ROADMAP findings) |
| Tracking (IoU + constant velocity) | Built and unit-tested |
| Temporal event engine (zone entry, dwell, repeated presence, sequence, multi-camera) | Built and tested |
| Cross-camera correlation (topology + timing + optional appearance; refuses ambiguous merges) | Built and tested |
| Incidents, corroboration, alert policy (arm modes, rate limit, escalation) | Built and tested |
| Event store (SQLite), clip and snapshot recording, retention purge | Built. Clip codec depends on the OpenCV build |
| Notifications (ntfy, MQTT, log) | Built. Push delivery to a real phone not yet drilled |
| REST + SSE + MJPEG API with token auth | Built and tested |
| Dashboard (live, map, timeline, drone, settings) | Built and smoke-tested |
| Drone: simulated adapter, geofence, approval workflow, safety monitor | Built and tested |
| Drone: MAVLink adapter | **Telemetry only. All flight commands are refused by design** |
| Accounts/multi-user, mobile app, cloud relay, HTTPS deployment | Not started (see `ROADMAP.md`) |

## Repository map

```text
src/sentinel/            the Sentinel brain (current product)
  models.py              Detection, Track, SecurityEvent, Incident, Notification, severity helpers
  config.py              site config loading, validation, ${ENV} secret substitution, URL redaction
  tracker.py             per-camera IoU tracker
  engine.py              TemporalEventEngine + ZoneMap (zone hysteresis, rules)
  correlation.py         CrossCameraCorrelator, SiteTrack (cross-camera / re-acquisition linking)
  incidents.py           IncidentManager (grouping, corroboration) + AlertPolicy
  store.py               SQLite events / incidents / audit + retention purge
  notify.py              ntfy / MQTT / log notifiers on a background dispatcher
  vision.py              frames, privacy masks, motion gate, appearance histogram, annotation
  sources.py             OpenCV capture with latest-frame reader and reconnect backoff
  detectors.py           Detector protocol, MediaPipe object detector, scripted (sim) detector
  recorder.py            pre-roll ring buffer + MP4 clips + snapshots
  simulation.py          scripted multi-camera scenarios and simulated camera source
  pipeline.py            CameraWorker thread: capture -> mask -> gate -> detect -> track -> engine
  runtime.py             SentinelRuntime hub + EventBus (events -> incidents -> store/bus/alerts)
  replay.py              deterministic offline scenario replay (tests, rule tuning)
  service.py             process assembly: runtime + workers + housekeeping + drone loop + MQTT
  api.py                 FastAPI app (/api/v1) + dashboard host
  mqtt.py                MQTT mirror of bus traffic
  drone/                 base (adapter ABC, telemetry), mock (simulator), geofence, manager, mavlink
  web/                   dashboard (static HTML/CSS/JS, no build step)
  bench/                 real-footage benchmark: labels (MOT/CAVIAR/Sentinel), runner (offline), live,
                         metrics, degrade (synthetic stress), report, render + viewer (playback/debug UI)
config/sentinel.example.json   demo site: 3 simulated cameras, zones, topology, rules, drone
config/scenarios/              scripted scenarios (delivery, prowler, ambiguous handoff, demo loop)
config/bench/                  benchmark site config (zones per public clip), manifest, live RTSP example
scripts/fetch_bench_footage.py downloads public annotated footage into data/bench/ (git-ignored)
tests/test_sentinel_*.py       Sentinel tests (tests/sentinel_helpers.py holds the fixtures)
docs/architecture.md           architecture and event-processing design
docs/api.md                    API reference for dashboard and mobile clients
docs/drone-integration.md      drone design, safety gates, ArduPilot/MAVLink path
docs/benchmarking.md           how to measure Sentinel on real footage, and what each metric means
ROADMAP.md                     phased implementation roadmap
sources.md                     license and reuse audit
src/senior_safety/, webapp/, scripts/, integrations/, data/   legacy CameraAI (frozen)
```

## Commands

Run all commands from the repo root. `src` must be on `PYTHONPATH`.

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests                       # all tests: Sentinel plus legacy
python -m unittest discover -s tests -p "test_sentinel_*.py"
python -m sentinel validate --config config/sentinel.example.json
python -m sentinel replay --scenario config/scenarios/prowler.json --at 2026-10-12T02:00:00
python -m sentinel serve                                   # dashboard on http://127.0.0.1:8080
python scripts/fetch_bench_footage.py                      # public annotated footage (~600 MB)
python -m sentinel.bench run --all --out bench_runs/NAME   # real-footage benchmark -> report.md/html
python -m sentinel.bench view --runs bench_runs            # playback/debug viewer on :8090
```

Install dependencies with `pip install -r requirements.txt`. The engine, tracker, correlator, incidents, store, and drone manager use only the standard library. OpenCV, MediaPipe, FastAPI, and paho are needed only by the I/O edges. Keep it that way so the core stays testable anywhere.

## Architecture in one paragraph

Each camera runs a `CameraWorker` thread. The worker applies privacy masks, skips the detector on static scenes (motion gate), runs the detector, drops tiny or ignore-zone boxes, and feeds a per-camera `IoUTracker`. Confirmed tracks go to the shared `SentinelRuntime`, which calls `TemporalEventEngine.process()`. The engine maps track ground points (bottom-centre of the box) into zones with enter/exit hysteresis. The correlator joins camera tracks into site tracks. The engine then evaluates rules over time and emits `SecurityEvent`s that carry reason codes, durations, and confidence. `IncidentManager` groups events by site track or camera, and `AlertPolicy` decides whether a push is warranted. Everything is persisted to SQLite and published on an in-process `EventBus`. The API (SSE), the MQTT mirror, and the dashboard are all bus subscribers. Full detail is in `docs/architecture.md`.

## Non-negotiable product boundaries

1. **Describe behaviour, never intent or identity.** Event and alert text says what was observed ("Person entered Shed", "3 separate visits to Front porch in 15 min"). Never use "intruder", "burglar", "suspicious person", "criminal", or a named identity. Every notification ends with the observed-activity disclaimer (`incidents.DISCLAIMER`).
2. **Never merge identities on weak evidence.** Cross-camera links need topology, timing, and no comparable competing candidate. When unsure, start a new site track and record `ambiguous_with`. Repeated-presence events state that visits "may be by the same or different people". Appearance vectors are coarse colour histograms used only as a tie-breaker. No face recognition, licence-plate recognition, or biometric identification without an explicit product, legal, and privacy decision recorded here first.
3. **Alerts must stay trustworthy.** Notify once per incident, again only when its severity rises, with a global rate limit. Ordinary activity is logged as `info` and never pushed. False-alarm cost is a first-class metric.
4. **Privacy by default.** Processing is local. Privacy masks are applied before detection, recording, and streaming. Clips and snapshots stay on local disk under retention limits. Snapshots go to third-party push services only when `attach_snapshot` is explicitly enabled. Never commit media, recordings, `data/sentinel/`, or real site configs.
5. **No public camera exposure.** The API binds to `127.0.0.1` by default and refuses to bind elsewhere without `SENTINEL_API_TOKEN`. Camera source URLs are never returned by the API. Remote access goes through a VPN or an authenticated HTTPS reverse proxy, never port-forwarding.
6. **Secrets live in the environment.** Config files use `${ENV_VAR}` for camera passwords, ntfy topics, and tokens. Validation rejects literal passwords in RTSP URLs. MQTT credentials come from `MQTT_USERNAME`/`MQTT_PASSWORD`.
7. **Drone safety.** No autonomous real-world flight. Sentinel may *request* inspections, but only an operator can approve them. Every command is checked against the geofence and written to the audit log. The flight controller owns stabilisation, failsafes, fences, and landing. Do not write custom flight controllers or landing algorithms. The MAVLink adapter stays telemetry-only until every gate in `docs/drone-integration.md` is met and recorded.
8. **No automatic calls to police or emergency services.** Escalation goes to people only.

## Technical decisions (and why)

- **Detector: MediaPipe EfficientDet-Lite (Apache-2.0).** It is already a dependency, it runs on CPU, and it is commercially safe. Ultralytics YOLO is AGPL and must not ship in a closed product without a commercial licence. Any other detector plugs in behind `detectors.Detector.detect(frame) -> list[Detection]`. Good candidates: ONNX Runtime or OpenVINO running Apache/MIT-licensed weights such as RT-DETR or YOLOX, for GPU or NPU acceleration.
- **CPU practicality.** A motion gate skips detection on static frames, an idle re-detect runs at 1 s, frame rate is capped per camera (default 5 fps), the reader thread keeps only the latest frame, and pre-roll is stored as JPEG. GPU is opt-in through `detector.delegate: "gpu"` where MediaPipe supports it (not on Windows).
- **Tracker: our own IoU tracker.** It is small, dependency-free, and has time-based expiry. Move to ByteTrack or OC-SORT behind the same `update()` contract if crowded scenes require it.
- **Storage: SQLite (WAL) plus files on disk.** Zero-ops and single-site. The store API is narrow so Postgres can replace it for multi-site hosting.
- **API: FastAPI + uvicorn.** Typed request models, OpenAPI at `/api/docs` for future mobile clients, async SSE and MJPEG. Bearer token for apps, exchanged for an HttpOnly cookie for the dashboard. Tokens never go in URLs.
- **Dashboard: static HTML/JS with no build step.** It matches the webapp precedent, and the API is the contract. A native mobile app will consume the same `/api/v1`.
- **Event-driven core.** The EventBus is in-process. MQTT mirrors it for Home Assistant and other consumers. Swap to NATS or Redis streams only when there are multiple processes.
- **Time.** Engine logic uses event timestamps only, never wall-clock reads, so replay is deterministic. Rule `active_hours` use the host's local time zone.
- **Reuse from the legacy code.** `senior_safety.pose_events.point_in_polygon`, `senior_safety.config_validation.ConfigValidationError`/`read_json_object`, the ntfy and MQTT conventions, and the alert-lifecycle ideas (dedupe, escalation, audit).

## Working conventions

- Every new rule type, event type, or alert behaviour needs a unit test, and ideally a scenario in `config/scenarios/` with an assertion in `tests/test_sentinel_scenarios.py`. Tune rules with `replay` before touching thresholds in live config.
- **Detection, tracking, or rule changes must be re-measured with `sentinel.bench` against the previous run, and the result recorded in the ROADMAP findings.** Never quote accuracy without naming the labeled footage behind it. Synthetic degradations and unlabeled clips never support accuracy claims.
- Keep the core (`models`, `tracker`, `engine`, `correlation`, `incidents`, `store`, `drone/manager`) free of OpenCV, numpy, and FastAPI imports.
- New config keys must be validated in `config.validate_config` and documented in `docs/architecture.md`.
- API changes are additive under `/api/v1`. Breaking changes go to `/api/v2`. Update `docs/api.md`.
- Match the surrounding style: dataclasses, type hints, small modules, comments only where the *why* is non-obvious.
- Do not modify `src/senior_safety/` or `webapp/` except to keep shared helpers working. Do not delete them without asking.
- Run the full test suite before committing.

## Roadmap pointer

Phases and exit criteria are in `ROADMAP.md`. In short: (1) brain on real cameras, (2) pilot hardening (accounts, HTTPS, service install, health), (3) mobile app, (4) evaluation dataset and detector upgrades, (5) drone via SITL then a supervised tethered or bench rig, (6) multi-site and commercial readiness.
