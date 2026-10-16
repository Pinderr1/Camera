# Sentinel: development guide

This is the authoritative guide for Claude Code and Codex sessions in this repo. `AGENTS.md` points here. If code and this file disagree, fix one of them in the same change.

## What Sentinel is

Sentinel is a local-first, multi-camera security system for homes and small businesses. Its job is **continuous situational awareness, not isolated motion alerts**. It tracks people and vehicles over time and across cameras, reasons about sequences of behaviour inside configured property zones, groups related observations into incidents, and notifies a person only when an incident is worth their attention.

A drone will later act as Sentinel's **mobile camera, not its brain**. Sentinel proposes an inspection, a human operator approves it, and a certified flight stack (ArduPilot/PX4) flies it.

Long-term goal: a commercially viable platform for residential properties and small businesses. Every licensing, privacy, and security decision should hold up for a product that is sold.

History: the repo started as CameraAI, a senior night-safety monitor (`src/senior_safety/` plus the BedWatch phone app in `webapp/`). That direction ended in October 2026. Its documents live in `docs/history/senior-night-safety/`. Its code is frozen but kept, because Sentinel reuses parts of it.

## Branch and workspace

- **`main` is the primary development branch.** All Sentinel work happens on `main` in the original repository folder (`CameraAI`). Do not create extra worktrees or development folders.
- The `sentinel` and `network-diagnostics` branches are historical; their work is fully merged into `main` (up to a08ef62). Do not develop on them.
- Use a short-lived feature branch off `main` only when asked, and merge it back into `main`.
- The working tree may hold the owner's uncommitted changes to unrelated files (legacy configs, labels, data). Never commit, stash, reset, or discard them unless asked. Stage only the files your change touches.
- Commit and push only when asked.

## Current status (October 2026)

| Area | Status |
| --- | --- |
| Multi-camera ingestion (RTSP/HTTP stream+snapshot/USB/capture/file/simulated) | Built: sub-stream preference, hardware decode with fallback, reconnect, diagnostics. Verified against MediaMTX and fake RTSP/ONVIF servers. **No physical camera or DVR tested yet** |
| Camera onboarding wizard + ONVIF discovery | Built and tested end to end in a real browser (Edge) against recorded footage |
| Network troubleshooter (`netdiag`: network inspection, SADP/ONVIF/port discovery, diagnosis with evidence, channel search) | Built. Windows collector checked on the real install network; diagnosis verified on simulated networks and in Edge (Playwright). **No SADP reply from a real device yet** |
| Visual setup editor (cameras, zones, privacy masks, property map, connections, categories) | Built. Edits are validated, persisted to `<storage>/site.json`, and applied live |
| Credentials | DPAPI-encrypted store on Windows (0600 file elsewhere); never in config, API responses, or logs |
| Environmental intelligence | Category filters (detect/alerts per category and camera). Scene conditions and unknown-motion via motion analysis. Validated on synthetic sequences only |
| Real-footage benchmark (`sentinel.bench`) | Built: recorded and live feeds, accuracy against annotations, latency, CPU/memory, multi-camera sessions, playback viewer, reports |
| Object detection (ONNX Runtime: YOLOX default, RT-DETRv2/D-FINE/RF-DETR options; EfficientDet fallback) | Built and benchmarked. Default YOLOX-s @960 on GPU: about 74% person recall at about 95% precision on MOT16-09. Crowded MOT16-02 is still about 30% (see ROADMAP) |
| Tracking (IoU + constant velocity) | Built and unit-tested |
| Temporal event engine (zone entry, dwell, repeated presence, sequence, multi-camera) | Built and tested |
| Cross-camera correlation (topology + timing + optional appearance; refuses ambiguous merges) | Built and tested |
| Incidents, corroboration, alert policy (arm modes, rate limit, escalation) | Built and tested |
| Event store (SQLite), clip and snapshot recording, retention purge | Built. Clip codec depends on the OpenCV build |
| Notifications (ntfy, MQTT, log) | Built. Push delivery to a real phone not yet drilled |
| 24/7 reliability: coverage health (offline/frozen/AI-failed/stalled), worker supervision with backoff and restart budget, GPU→CPU detector fallback, outage history, coverage notifications, disk guard and protected recordings, log rotation, backups and startup recovery, System tab | Built and tested with simulated faults (`scripts/reliability_smoke.py`, failure-scenario tests, Edge UI tests). **Not yet run for days on real cameras** |
| Background running: `sentinel run` process supervisor, graceful stop, Windows Task Scheduler install, systemd unit | Supervisor and graceful stop tested with real processes. `service install` generates and is unit-tested but has not been installed on a machine yet |
| REST + SSE + MJPEG API with token auth | Built and tested |
| Dashboard (live, map, timeline, drone, settings) | Built and smoke-tested |
| Drone: simulated adapter, geofence, approval workflow, safety monitor | Built and tested |
| Drone: MAVLink adapter, parameter/fence audit, readiness, propose mode (approval exports a `.plan` the pilot flies; Sentinel follows from telemetry), flight and maintenance logs, restart recovery | Built and **tested against ArduPilot SITL (ArduCopter 4.7.2)**. Real aircraft: **telemetry only, every flight command refused** |
| Drone: SITL command adapter | Commands only with `allow_flight_commands` + loopback link + SIM_* parameters; SITL-tested (inspection, link-loss FC failsafe, low-battery return) |
| Drone companion (relay with command filter, pairing, HMAC sessions, health agent, Pi provisioning) | Built and tested on the Hub and against SITL. **Not yet run on a Raspberry Pi** |
| Drone video and observations (detection on the Hub, tracking, ground position with uncertainty, evidence on the incident) | Built; tested with a simulated aerial camera posed by SITL. **No real aerial footage** |
| Drone V1 hardware | Architecture and prototype plan (`docs/drone-hardware-v1.md`): X650 + Pixhawk 6X/CM4 baseboard + ZT6 + HM30 + ExpressLRS + SF45/B (now the Sentinel Thermal reference). **No hardware bought, no physical flight** |
| Drone models: Sentinel Lite (cheapest: X500 V2 dev kit, Pixhawk 6C, A8 mini, HM30, no companion), Pro, Thermal (`docs/drone-models.md`) | Capability catalog driving registration, model-specific readiness (command lockout, companion, camera stream, daylight, wind/temperature, model FC settings), route endurance check, video settings, simulator performance, troubleshooting, comparison screen and AI Guide. Every model tested in the simulator; **Lite tested in ArduPilot SITL with its own commissioning (FC `MAV_OPTIONS` lockout, passive audit over a shared radio)**; Pro/Thermal profiles in SITL. Reference designs only: **nothing built or flown** |
| Drone reliability: telemetry validation (rejected (0,0)/out-of-range positions and implausible batteries, position jumps, per-signal ages, FC restart confirmed over three messages, other autopilots, signing detection, added link delay, the FC's battery and fence verdicts), flight-health monitor, nav-loss and stale-data aware readiness and advisories (never "returning home" without navigation), contained record failures, lost-contact timeout, read-only command-path probe, relay accepts only its configured peers | Built and unit-tested; failure scenarios run against ArduPilot SITL 4.7.2 (`scripts/sitl/fault_scenarios.py`, results in `docs/drone-reliability.md`). **No physical aircraft** |
| Drone video coverage: frozen/blank frames rejected, coverage (`none`/`partial`/`full`) only from proven-live frames over the point, pre-flight stream check (frames discarded), stale or slewing gimbal not used | Built and tested (unit, SITL-posed simulated camera with injected freezes/drops) |
| Drone hardware bench test (`sentinel.drone.bench`, propellers off, never arms, no flight commands) and setup journey (planned → simulated → detected → connected → configured → bench verified → flight-test verified → operationally authorized, from evidence and named records) | Built; bench run against SITL as a stand-in. **Never run on real hardware** |
| Sentinel Lite cost-down study (`docs/drone-lite-cost.md`): firmware features verified per board, three BOMs, Canadian landed cost, manufacturing and purchasing sequence | Research only; **nothing bought** |
| Optional drone accessories and FLAME Mode (agricultural burn monitoring on the same aircraft): accessory registry with compatibility records (mass, endurance, sensors, interface), commissioning, inspections, simulated health and faults; ground-only switching between security and agricultural configurations (each refuses the other's requests); fields, watch strips, sensitive places; checklists, crew, equipment, permits/qualifications (recorded, not verified); weather against operator limits and smoke direction; thermal watch with coverage and position uncertainty; notifications; fingerprinted reports; FLAME tab; AI Guide tools (`docs/flame.md`, `docs/flame-hardware-evaluation.md`) | Built; unit, API, Guide and Edge tests; **SITL scenario on ArduCopter 4.7.2 (Thermal parameters + simulated SF45/B)** with simulated fire, thermal camera and kit. **No accessory built; no real thermal ingestion; no ignition hardware (excluded)** |
| Camera liveness: independent frame, decoder, stream-clock, picture and inference signals; still scenes are `unverified`, never falsely `frozen`; recorder-clock freezes detected | Built and tested on simulated cameras with sensor noise and overlay freezes. **Thresholds not yet checked on real recorders** |
| Evidence-preserving database recovery (originals moved aside with hashes, salvage from a copy, backup age and loss window reported and notified) | Built and tested |
| Rule builder (types incl. combinations; modes Home/Away/Night/Vacation; days; cooldowns; notification preference; minimum observations; templates) with mandatory test before enabling (simulated walk, scenarios, recorded activity) | Built; backend and Edge tests. Recorded-activity tests are approximations |
| Explainable incidents (observed vs inferred, rules, confidence, severity reasons, evidence) with visual timeline and replay sync | Built and tested on replays |
| Activity insights (patterns, changes, unusual observations as information only, approval-only suggestions) | Built; tested on synthetic history only |
| Investigation: search, notes, feedback labels, nuisance reports per rule/camera, missed-event reports, fingerprinted exports | Built and tested |
| Installation journey (Install tab): four paths (existing cameras, new cameras, drone-first, complete package), ten saved steps, readiness that separates registered / connected / video / AI verified, completion with acknowledged issues | Built; backend, service and Edge tests on simulated cameras, drone and sensors. **Not yet used with physical hardware or by a non-technical person** |
| Device registry: recorders, drones, docks, sensors beside cameras; hardware compatibility profiles with support levels; sites with no cameras; several drones (primary takes inspections) | Built and tested. Only this computer is `verified`; every camera/recorder profile is `expected` or `limited`; sensors are simulated or planned (registration only, never incidents) |
| Property planner: features, cameras with aim/FOV, sensors, network, drone docks/points; approximate coverage with line of sight, gaps, privacy warnings; camera suggestions; PoE/cable estimates; printable plan | Built and unit-tested. **Approximate geometry; never compared with a real installation** |
| Sentinel AI Guide (Guide tab): conversational setup, troubleshooting with retest, rule configuration, everyday questions; read tools on live data, proposals confirmed by a person, secure sign-in form; built-in offline guide plus optional DeepSeek (default external) or Claude provider with address placeholders | Built; backend tests (scripted provider, fake DeepSeek and Messages APIs) and Edge tests on simulated hardware; DeepSeek checked against the live API on a simulated site. **Not yet run against the live Claude API or real hardware** |
| Wilderness Guardian Phase 2 (`guardian` branch): animal movement intelligence (persistent guardian tracks with unambiguous re-acquisition, approach/retreat/lateral/stationary from box growth and image motion, approach to camera/protected zone/person, turns, sustained approach, calibrated-only distance and time-to-zone, explainable levels Observed/Approaching/Elevated Concern/Urgent Review, cooldowns, clip evidence, live overlay, `/api/v1/guardian`). Off by default (`docs/wilderness-guardian.md`) | Built; **synthetic sequences only** (`tests/test_sentinel_guardian.py`, `config/scenarios/wildlife_bear_approach.json`). **No real wildlife footage yet; no species classifier (COCO labels shown as unconfirmed)** |
| Guardian wildlife datasets (`guardian` branch): licence-gated importers (JSON registry `config/guardian/datasets.json`; streaming CSV for NACTI), explicit label maps with species/uncertain/negative/excluded, location splits, SHA-256 duplicates, byte budget, provenance manifest, `docs/guardian-datasets.md` | Built; offline unit tests. **435 images downloaded (ENA24, Caltech, NACTI; CDLA-Permissive-1.0): black bear, cougar, coyote, moose, elk, negatives. No wolf or grizzly images (no grizzly source in any LILA dataset; WSU Lynx registered for wolves).** No model trained |
| Guardian species recognition (`guardian` branch, optional, off by default): Google SpeciesNet v4.0.3a crop classifier (Apache-2.0) exported to ONNX, on Sentinel's YOLOX boxes (no MegaDetector); background worker with bounded queue, per-track caching and periodic reclassification; confirmed/uncertain/unknown with taxonomy roll-up and geofence; never lowers concern; species-specific guidance only for confirmed species; events, overlay, API, health component | Built; unit and runtime tests with a fake model, plus a real-model test. **Real inference on 435 camera-trap images: 160/161 confident calls right on Sentinel boxes, but YOLOX found an animal in only 43 %.** Measured on an i5-1135G7 / Iris Xe (342 ms per crop on DirectML). **Not run on owner camera footage or on an NVIDIA GPU** |
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
  sources.py             capture: OpenCV (RTSP/HTTP/USB/capture/file) + HTTP snapshot poller, hw decode, diagnostics
  streams.py             brand templates, URL building from credential references, safe public source view
  onboarding.py          connection tests (RTSP-level probe + decoder test), local devices, decode capabilities
  onvif.py               WS-Discovery, ONVIF device/media profiles/stream URIs (stdlib)
  netdiag/               network troubleshooter (read-only): collect (Windows PowerShell / Linux ip -j), model (snapshot,
                         routes, target policy, findings), probes (TCP, ICMP, HTTP id), sadp (Hikvision discovery),
                         diagnose (rules + discovery), guidance (brand/OS fix steps), simulated (scenario networks)
  credentials.py         SecretStore: DPAPI (Windows) / 0600 file credential storage
  site_config.py         SiteConfigManager: validated, persisted, live-applied config edits; drafts; cascades; devices, drone setup, notification channels
  devices.py             device registry (stdlib): categories, capabilities, hardware profiles + support levels, validation, live levels, simulated sensors
  planner.py             property planner (stdlib): plan validation, coverage with line of sight, recommendations, PoE/cable estimates, checklists, report
  installation.py        installation journey (stdlib): saved progress (<storage>/installation.json) and honest readiness checks
  assistant/             Sentinel AI Guide: tools (read / ui / propose), core (conversation + provider loop), guide (offline),
                         provider (selection; Anthropic via optional SDK), deepseek (OpenAI-format Chat Completions over httpx),
                         privacy (secret scrubbing, address placeholders), store, routes, settings
  environment.py         scene conditions (light, IR, rain, shadows, vegetation, lens) + unknown-motion proposals
  detectors.py           Detector protocol, hardware profiles (`model: auto`), tiling, NMS, MediaPipe + scripted detectors
  detectors_onnx.py      ONNX Runtime detectors (YOLOX, DETR family), model registry with licence/training-data metadata
  recorder.py            pre-roll ring buffer + MP4 clips + snapshots
  simulation.py          scripted multi-camera scenarios and simulated camera source
  pipeline.py            CameraWorker thread: capture -> mask -> gate -> detect -> track -> engine; heartbeat, metrics, GPU->CPU fallback
  health.py              HealthMonitor: per-camera monitoring state, outages, coverage, health notifications (stdlib)
  supervisor.py          WorkerSupervisor: restart crashed/stalled workers, backoff, restart budget, circuit breaker (stdlib)
  storage_guard.py       free-space guard: oldest-first purge of unprotected media, recording pause/resume
  resources.py           CPU/memory/disk/NVIDIA GPU sampling (stdlib + ctypes)
  backup.py              backups (config, settings, SQLite), verify, restore, staged restore, damaged-database recovery
  daemon.py              run state, log rotation, stop requests, `sentinel run` process supervisor, Task Scheduler/systemd
  runtime.py             SentinelRuntime hub + EventBus (events -> incidents -> store/bus/alerts) + health wiring
  replay.py              deterministic offline scenario replay (tests, rule tuning)
  rules.py               rule-builder fields, plain-language descriptions, templates (stdlib)
  rule_lab.py            test a rule before enabling it: simulated walk, scenario, or recorded visits through the real engine
  explain.py             structured incident explanations: observed vs inferred, rules, confidence, severity, evidence (stdlib)
  insights.py            descriptive activity statistics, unusual observations (information only), suggestions (stdlib)
  reports.py             nuisance/performance reports from feedback; incident export with evidence fingerprints
  service.py             process assembly: runtime + supervised workers + housekeeping (health, storage, backups) + drone loop + MQTT
  api.py                 FastAPI app (/api/v1) + dashboard host
  mqtt.py                MQTT mirror of bus traffic
  drone/                 base (adapter ABC, telemetry), mock (simulator), geofence, manager (modes, readiness, phases, logs),
                         catalog (drone models Lite/Pro/Thermal: capabilities, requirements, estimates; stdlib), troubleshoot (stdlib),
                         mavlink (telemetry-only + SITL-only command adapters), ardupilot (modes, flight state, parameter audit),
                         mission (routes, .plan/.waypoints export), pairing (companion identity), observation (pose, ground
                         projection, inspection observations, video coverage; stdlib), video (drone video worker, quality monitor,
                         simulated aerial camera), flight_health (what telemetry supports claiming; stdlib), commissioning (setup
                         journey and levels; stdlib), faults (fault-injection framework: FaultLink, FaultyVideoSource, scenario
                         runner and report), bench (propeller-free hardware bench test CLI), accessories (removable accessory
                         profiles, compatibility evaluation, configuration-switch rules; stdlib)
  flame/                 FLAME Mode (stdlib core): fields, operations (checklist, weather, classification, readiness wording),
                         thermal (simulated fire + thermal observer), service (FlameService: accessories, configurations, monitoring
                         loop, notifications, <storage>/flame.json), reports, routes (/api/v1/flame), assistant (Guide tools/answers)
  companion/             drone companion computer (stdlib): MAVLink relay with Hub command filter, health agent, pairing CLI, Pi provisioning
  guardian/              Wilderness Guardian (stdlib): monitor (guardian tracks, targets, levels, events, overlays), movement (relative
                         kinematics), risk (species wording, explainable scoring), geometry (polygon distance, ground homography), validate,
                         datasets (licence-checked wildlife dataset importer; registry config/guardian/datasets.json, see docs/guardian-datasets.md),
                         dataset_stream (row-by-row CSV metadata for huge datasets), species (stdlib species decisions: roll-up, geofence,
                         per-track aggregation), species_onnx (SpeciesNet on ONNX Runtime + background worker; edge), species_service (glue)
scripts/guardian/              export_speciesnet_onnx.py (one-time ONNX export, separate torch env), speciesnet_eval.py (real-image species report)
  web/                   dashboard (static HTML/CSS/JS, no build step); setup.js = wizard + editors; install.js = Install tab + planner
  bench/                 real-footage benchmark: labels (MOT/CAVIAR/Sentinel), runner (offline), live,
                         metrics, degrade (synthetic stress), report, render + viewer (playback/debug UI)
config/sentinel.example.json   demo site: 3 simulated cameras, zones, topology, rules, drone
config/sentinel.drone-first.example.json   drone-first demo: no cameras, simulated drone, dock and sensors
config/sentinel.farm.example.json   FLAME demo: simulated Sentinel Thermal drone, dock and burn-monitoring kit on a farm map
config/drone/sentinel-thermal-sitl.parm   Thermal commissioning for SITL (V1 failsafes + simulated SF45/B on SERIAL5)
config/scenarios/              scripted scenarios (delivery, prowler, ambiguous handoff, demo loop)
config/netsim/                 simulated network conditions for the troubleshooter (regression: Hikvision on another subnet)
scripts/sitl/                  start_sitl.sh (official ArduCopter SITL in Linux/WSL2), gcs_standin.py (scripted commissioning engineer and pilot; simulators only)
scripts/drone_sitl_demo.py     end-to-end drone demo: incident -> request -> approval -> SITL flight -> drone video -> evidence
scripts/sitl/start_sitl_instance.sh  a separate SITL instance (-I N, own directory, never kills other SITLs; several parameter files)
scripts/sitl/fault_scenarios.py      drone failure scenarios against SITL (GPS, fence, battery, radio, video, operations) -> report.md/results.json
config/drone/sentinel-lite-gimbal-sitl.parm  overlay: SITL's simulated SIYI gimbal on SERIAL5 through ArduPilot's SIYI driver
config/drone/sentinel-v1-sitl.parm   commissioning parameters (failsafes, fence, RTL) used with SITL
config/drone/sentinel-lite-sitl.parm commissioning for Sentinel Lite: FC command lockout (MAV_OPTIONS), MAV2_* stream rates on the radio port
scripts/reliability_smoke.py   one-minute failure drill on simulated cameras (disconnect, freeze, crash, stall, AI, disk, restart, DB)
scripts/sentinel_launcher.py   starts Sentinel without PYTHONPATH (used by the Windows task)
config/bench/                  benchmark site config (zones per public clip), manifest, live RTSP example
scripts/fetch_bench_footage.py downloads public annotated footage into data/bench/ (git-ignored)
tests/test_sentinel_*.py       Sentinel tests (tests/sentinel_helpers.py holds the fixtures); test_sentinel_ui.py drives Edge via Playwright
docs/architecture.md           architecture and event-processing design
docs/api.md                    API reference for dashboard and mobile clients
docs/drone-integration.md      drone architecture (Hub / companion / FC), modes, inspection workflow, video, pairing, config, SITL results, gates
docs/drone-hardware-v1.md      drone V1 hardware: recommended architecture, alternatives, budgets, wiring, bench tests, BOM, product readiness, Canadian rules
docs/drone-models.md           Sentinel Lite / Pro / Thermal: cheapest-drone research, tiers, shared parts, BOMs, smallest bench purchase, model software
docs/drone-lite-cost.md        Lite cost-down: verified board firmware features, three BOMs, landed cost, manufacturing, purchasing sequence
docs/drone-reliability.md      drone failure handling, flight health, lockout findings, video coverage, fault framework and SITL results
docs/drone-bench-testing.md    propeller-free hardware bench test, supported firmware, acceptance criteria before purchase and on the bench
docs/flame.md                  optional accessories and FLAME Mode: scope, simulation limits, persistence, dashboard/API/Guide, validation
docs/flame-hardware-evaluation.md  X650 payload/endurance budget, IGNIS III Mini lessons, regulation notes, engineer's validation list, blocked items
docs/benchmarking.md           how to measure Sentinel on real footage, and what each metric means
docs/camera-setup.md           onboarding, DVR fallbacks, diagnostics, credentials, editors
docs/environmental-intelligence.md  category audit, missing classes/datasets/licences, environment analysis
docs/reliability.md            24/7 operation: health states, outages, recovery, background start, storage, backups
docs/assistant.md              Sentinel AI Guide: modes, tools, confirmations, privacy, provider configuration
docs/installation.md           Install tab: four paths, readiness levels, device registry and profiles, planner, drone-first
docs/wilderness-guardian.md    Wilderness Guardian: five phases, datasets and licences, missing data, Phase 2 design, limits, camera tests
docs/intelligence.md           rules and rule builder, rule testing, explanations, activity insights, investigation and feedback
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
python -m sentinel serve --simulate-network config/netsim/hikvision_wrong_subnet.json   # troubleshooter demo
python -m sentinel serve --config config/sentinel.drone-first.example.json   # drone-first install, no cameras
python -m sentinel run --config config/sentinel.local.json --env-file <secrets.env>   # supervised: restarts after crashes
python -m sentinel service install|start|status|stop|uninstall --config ...   # Windows background start (Task Scheduler)
python -m sentinel backup create|list|verify|restore [NAME] --config ...
python scripts/reliability_smoke.py                       # failure drill, ~35 s, prints PASS/FAIL
bash scripts/sitl/start_sitl.sh 3                          # ArduCopter SITL (Linux/WSL2); then:
$env:SENTINEL_SITL='1'; python -m unittest tests.test_sentinel_drone_sitl   # SITL integration tests (~5 min)
# Sentinel Lite: start SITL with SITL_PARAMS=config/drone/sentinel-lite-sitl.parm (WSLENV=SITL_PARAMS/u from PowerShell), then:
$env:SENTINEL_SITL='lite'; python -m unittest tests.test_sentinel_drone_lite_sitl   # Lite lockout, passive audit, failsafes (~70 s)
python scripts/drone_sitl_demo.py --out bench_runs/drone_demo   # end-to-end drone inspection demo and report
bash scripts/sitl/start_sitl_instance.sh 2 3 config/drone/sentinel-lite-sitl.parm   # separate SITL (ports 5780-5783)
python scripts/sitl/fault_scenarios.py --fc tcp:127.0.0.1:5782 --out bench_runs/faults_lite   # drone failure scenarios + report
$env:SENTINEL_SITL='bench'; python -m unittest tests.test_sentinel_drone_bench_sitl   # bench test against SITL (instance 2)
# FLAME: start SITL with SITL_PARAMS=config/drone/sentinel-thermal-sitl.parm SITL_EXTRA_ARGS=--serial5=sim:sf45b (WSLENV both /u), then:
$env:SENTINEL_SITL='flame'; python -m unittest tests.test_sentinel_flame_sitl   # accessory, configuration switch, field, watch, faults, report (~1 min)
python -m sentinel serve --config config/sentinel.farm.example.json   # FLAME demo (simulated)
python -m sentinel.drone.bench run --connection udpin:0.0.0.0:14551 --model sentinel_lite --interactive   # real bench, propellers off
python -m unittest tests.test_sentinel_ui                  # browser tests (pip install playwright; uses installed Edge)
python scripts/fetch_bench_footage.py                      # public annotated footage (~600 MB)
python -m sentinel.bench run --all --out bench_runs/NAME   # real-footage benchmark -> report.md/html
python -m sentinel.bench view --runs bench_runs            # playback/debug viewer on :8090
bash scripts/bench_detectors.sh bench_runs/detectors       # detector comparison (same clips, same settings)
python -m sentinel.bench compare bench_runs/detectors/*    # comparison table across runs
```

Install dependencies with `pip install -r requirements.txt`. The engine, tracker, correlator, incidents, store, and drone manager use only the standard library. OpenCV, MediaPipe, FastAPI, and paho are needed only by the I/O edges. Keep it that way so the core stays testable anywhere.

## Architecture in one paragraph

Each camera runs a `CameraWorker` thread. The worker applies privacy masks, skips the detector on static scenes (motion gate), runs the detector, drops tiny or ignore-zone boxes, and feeds a per-camera `IoUTracker`. Confirmed tracks go to the shared `SentinelRuntime`, which calls `TemporalEventEngine.process()`. The engine maps track ground points (bottom-centre of the box) into zones with enter/exit hysteresis. The correlator joins camera tracks into site tracks. The engine then evaluates rules over time and emits `SecurityEvent`s that carry reason codes, durations, and confidence. `IncidentManager` groups events by site track or camera, and `AlertPolicy` decides whether a push is warranted. Everything is persisted to SQLite and published on an in-process `EventBus`. The API (SSE), the MQTT mirror, and the dashboard are all bus subscribers. A `WorkerSupervisor` restarts crashed or stalled camera workers, and a `HealthMonitor` decides per camera whether it is really monitoring, records outages, and notifies people when coverage is lost (see `docs/reliability.md`). Full detail is in `docs/architecture.md`.

## Non-negotiable product boundaries

1. **Describe behaviour, never intent or identity.** Event and alert text says what was observed ("Person entered Shed", "3 separate visits to Front porch in 15 min"). Never use "intruder", "burglar", "suspicious person", "criminal", or a named identity. Every notification ends with the observed-activity disclaimer (`incidents.DISCLAIMER`).
2. **Never merge identities on weak evidence.** Cross-camera links need topology, timing, and no comparable competing candidate. When unsure, start a new site track and record `ambiguous_with`. Repeated-presence events state that visits "may be by the same or different people". Appearance vectors are coarse colour histograms used only as a tie-breaker. No face recognition, licence-plate recognition, or biometric identification without an explicit product, legal, and privacy decision recorded here first.
3. **Alerts must stay trustworthy.** Notify once per incident, again only when its severity rises, with a global rate limit. Ordinary activity is logged as `info` and never pushed. False-alarm cost is a first-class metric.
4. **Privacy by default.** Processing is local. Privacy masks are applied before detection, recording, and streaming. Clips and snapshots stay on local disk under retention limits. Snapshots go to third-party push services only when `attach_snapshot` is explicitly enabled. Never commit media, recordings, `data/sentinel/`, or real site configs.
5. **No public camera exposure.** The API binds to `127.0.0.1` by default and refuses to bind elsewhere without `SENTINEL_API_TOKEN`. Camera source URLs are never returned by the API. Remote access goes through a VPN or an authenticated HTTPS reverse proxy, never port-forwarding.
6. **Secrets never sit in config, API responses, or logs.** Camera passwords entered in the dashboard go to the credential store, and configs reference them (`credential` / `${secret:NAME}`). Deployment secrets use `${ENV_VAR}`, which is never resolved into the persisted config. Validation rejects literal passwords in URLs. Stream URLs and paths are write-only over the API. MQTT credentials come from `MQTT_USERNAME`/`MQTT_PASSWORD`.
7. **Drone safety.** No autonomous real-world flight. Sentinel may *request* inspections, but only an operator can approve them. Every command is checked against the geofence and written to the audit log. The flight controller owns stabilisation, failsafes, fences, and landing. Do not write custom flight controllers or landing algorithms. The MAVLink adapter stays telemetry-only until every gate in `docs/drone-integration.md` is met and recorded. Flight commands exist only in `SitlMavlinkAdapter`, which requires `allow_flight_commands`, a loopback link and SIM_* parameters; never relax those checks, and never add commands to the telemetry-only adapter. Sentinel never writes flight-controller parameters. Drone video never feeds zone rules, and drone observations are never linked to fixed-camera identities. Every physical aircraft needs an independent Hub command lockout (companion relay with the `telemetry` policy, or the flight controller's `MAV_OPTIONS` lockout); never weaken the `lockout` readiness check. Drone models are reference designs: never describe one as built, flown, for sale, or with a measured flight time.
8. **No automatic calls to police or emergency services.** Escalation goes to people only.
9. **Network diagnostics are read-only and local.** Sentinel never changes router, firewall, computer, or device network settings; it explains what to change. It probes only private/link-local/CGNAT/loopback addresses, searches a network only after the person confirms they manage it (port checks at most the local /24), never collects MAC addresses, serial numbers, Wi-Fi names, or public IPs, and tries a recorder password once per test (accounts lock).
10. **Never claim coverage that does not exist.** A camera is reported as monitoring only while new frames reach a working detector and nothing shows the picture is frozen; a still picture that cannot be proven live is shown as `unverified`, not as fine and not as frozen. Offline, frozen, AI-failed, stalled, and restarting cameras are shown as not monitoring (Live overlay, coverage badge, System tab, API), recorded as outages, and pushed to people when they last. Health events never open security incidents, and health notifications do not depend on the arm mode.
11. **Registration is not connection, and nothing is "protected".** Registering a device records what is installed; it is reported as connected, delivering video, or AI-verified only from data Sentinel received. Planned hardware gets no adapter and is never shown as connected. Simulated devices are always labelled. The strongest statement Sentinel makes is "AI monitoring verified on N of M cameras"; it never tells a person their property is protected or secure. Planner coverage is approximate and always carries its assumptions; no prices, purchasing claims or coverage guarantees.
12. **Accessories are monitoring only; Sentinel never controls ignition.** No ignition, incendiary, fuel-delivery or release command, route, tool or control exists anywhere in Sentinel. Ignition payloads are evaluation records (`unsupported`) and cannot be registered as installed; improvised ignition devices are never designed or described. FLAME text never says or implies that a burn is approved, permitted, legal or safe to light, or that a fire is contained or out (`flame.operations.FORBIDDEN_CLAIMS`, tested on every generated text); permits, qualifications and checklist items are records of what people entered. Thermal observations state their coverage and position uncertainty. Burn notifications go to people only (boundary 8). The aircraft switches configuration only on the ground, and each configuration refuses the other's requests.

## Technical decisions (and why)

- **Detector: YOLOX via ONNX Runtime (Apache-2.0 code and weights), chosen by measurement.** `model: auto` uses YOLOX-s at 960x544 on any GPU provider (DirectML on Windows, CUDA, OpenVINO) and YOLOX-tiny at 416 on CPU. EfficientDet-Lite0 (MediaPipe) remains the fallback when ONNX Runtime is absent. RT-DETRv2 and D-FINE (Apache-2.0, COCO-only weights) are supported alternatives. RF-DETR is benchmark-only (Objects365 pre-training needs legal review). Ultralytics YOLO (AGPL) and YOLOv6/7/9 (GPL) are excluded. **Check weights licences separately from code licences**, and record them in `detectors_onnx.MODELS` (tests enforce the fields).
- **Hardware practicality.** A motion gate skips detection on static frames, an idle re-detect runs at 1 s, frame rate is capped per camera (default 5 fps), the reader thread keeps only the latest frame, and pre-roll is stored as JPEG. ONNX sessions are shared across cameras (one GPU copy, with DirectML calls serialised). Use camera sub-streams: full-rate 1080p decoding is the CPU bottleneck on older hosts.
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
- Keep the core (`models`, `tracker`, `engine`, `correlation`, `incidents`, `store`, `health`, `supervisor`, `drone/manager`, `drone/ardupilot`, `drone/mission`, `drone/pairing`, `drone/observation`, `companion/`) free of OpenCV, numpy, and FastAPI imports.
- Drone changes that touch MAVLink, routes or failsafes need a SITL test in `tests/test_sentinel_drone_sitl.py` (run with SITL up) besides unit tests.
- New config keys must be validated in `config.validate_config` and documented in `docs/architecture.md`.
- API changes are additive under `/api/v1`. Breaking changes go to `/api/v2`. Update `docs/api.md`.
- Match the surrounding style: dataclasses, type hints, small modules, comments only where the *why* is non-obvious.
- Config edits go through `SiteConfigManager` (validated, persisted, applied live); never mutate `runtime.config` directly.
- Environmental motion (scene conditions) must never open incidents; new environmental signals are `info` events.
- **Unusual is not a threat.** Activity statistics and anomaly indicators are information only: they never raise alerts, change severities, or change rules. Suggestions and feedback are applied only when the owner saves a rule. Feedback is for evaluation, never for silent retraining. Activity records hold categories, zones and times only (no images, positions, appearance, or identities).
- Rules can only be turned on from the builder after a test of the current version (the list switch asks for confirmation otherwise). New rule fields are validated in `config._validate_rule_extras` and described in `rules.describe_rule`.
- Never overwrite evidence: damaged or replaced databases are moved to `recovery/` with hashes before anything else happens, and every recovery is reported to the owner.
- New failure modes go through `HealthMonitor` (a camera state or a component), get a simulated fault or injected error, and a test in `tests/test_sentinel_reliability.py`. Run `scripts/reliability_smoke.py` after changing the pipeline, supervisor, or health code.
- Writes on the detection path must not raise into camera threads: contain the error, report it as a health component, and keep alerts flowing.
- New troubleshooter rules go in `netdiag/diagnose.py` with a `SimulatedNetwork` test. State `confirmed` only for faults Sentinel observed; anything inferred is `suspected` and must sit next to the alternatives.
- Do not modify `src/senior_safety/` or `webapp/` except to keep shared helpers working. Do not delete them without asking.
- New hardware kinds get a profile in `devices.PROFILES` with an honest support level (only hardware we tested is `verified`); never record prices or unchecked specifications. Device fields are validated in `devices.validate_devices`.
- Drone telemetry supports a claim only while it is current and plausible: readiness, safety states and advisories use `drone/flight_health.py` (staleness, navigation, the FC's own battery and fence verdicts), and Sentinel never says "returning home" when navigation has failed. New drone failure handling gets a scenario in `scripts/sitl/fault_scenarios.py` with explicit checks (finishing is not a result; what SITL cannot model is reported `not_modelled`), run on a separate SITL instance (`start_sitl_instance.sh`) so other sessions' simulators are untouched. Drone video counts as coverage only from proven-live frames.
- Drone models live in `drone/catalog.py`. Every figure there says where it came from (`source`) and estimates are labelled as such; prices go only in `docs/drone-models.md`. A new model or capability needs its readiness consequences tested per model (`tests/test_sentinel_drone_models.py`) and, if it changes MAVLink or failsafe expectations, a SITL test.
- Installation progress and plans live in `installation.json`, not the site config, so planning never restarts cameras. Readiness is computed from live data on every request; never store it as a claim.
- The assistant only reads through tools and changes things through `propose_*` tools that a person confirms; `Toolbox.apply` calls the same validated backend paths as the dashboard. Never add tools that run commands, read files, return secrets, change network or device settings, or command a drone. Passwords stay in the secure sign-in form, and addresses are aliased before anything reaches an external provider.
- Run the full test suite before committing.

## Roadmap pointer

**V1 software is feature-frozen (2026-10-15).** Until physical hardware testing is under way, change only what safety, hardware integration or a critical bug requires; no new features. Sentinel Lite is the first physical prototype. Known issues are listed in `ROADMAP.md` (V1 software feature freeze).

Phases and exit criteria are in `ROADMAP.md`. In short: (1) brain on real cameras, (2) pilot hardening (accounts, HTTPS, service install, health), (3) mobile app, (4) evaluation dataset and detector upgrades, (5) drone via SITL then a supervised tethered or bench rig, (6) multi-site and commercial readiness.
