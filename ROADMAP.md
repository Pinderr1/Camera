# Sentinel roadmap

Last updated: 2026-10-15. Priority: **a working security brain on real cameras before any drone hardware.** Sentinel runs unattended with honest coverage reporting and self-recovery (Phase 0.9), and its rules are now testable and its incidents explained (Phase 0.10); the next proof is days of continuous operation on real cameras. The detector upgrade raised MOT16-09 person recall from 28% to 74% at 95% precision. The next bottlenecks are crowded and occluded scenes, video decode cost, and the lack of labeled footage from real residential cameras.

## V1 software feature freeze (from 2026-10-15)

V1 software is frozen at the drone-reliability and accessories/FLAME commits of 2026-10-15. The next milestone is a real Sentinel device, not more simulation.

- **Allowed until physical hardware testing is under way:**
  - essential safety fixes;
  - hardware-integration changes the bench and prototype need;
  - fixes for critical bugs.
- **Not allowed:** new product features or experimental work in the V1 baseline. Experiments stay outside `main` until hardware results justify them.
- **Configurations kept as they are:** Sentinel Lite, Pro and Thermal (`drone/catalog.py`, `config/drone/*.parm`).
- **Hardware order:**
  - **Sentinel Lite** is the first physical prototype (Phase 5.1 below: bench stage 1, propellers off).
  - **Sentinel Rover**, a ground vehicle, is a possible second hardware platform. It is only an idea: no design, software or purchase exists, and it needs its own safety boundaries recorded in `CLAUDE.md` before any work.

### Verified at the freeze
- **Full regression, 45 modules run one at a time:**
  - 606 tests;
  - 0 failures;
  - 1 error (the AI Guide browser test below);
  - 13 skipped (SITL-only tests, which skip without a simulator).
- **SITL:** 45 of 48 drone failure scenarios pass on ArduCopter 4.7.2 with the Sentinel Lite parameters (`docs/drone-reliability.md` §6).
- **Evidence:** `bench_runs/recovery_20261015/` and `bench_runs/v1freeze_20261015/` (git-ignored; also in the preservation copies).

### Known issues and validation still required
- **Hardware-only scenarios.** Not validated in SITL: `companion_power_loss`, `fc_restart_in_flight`, `camera_latency`.
- **Flaky AI Guide browser test.** `tests/test_sentinel_assistant_ui.py::test_rule_proposal_cancel_resume_and_delete` fails intermittently, at different steps:
  - the Guide input is hidden;
  - the proposal card is empty;
  - "Cancelled" never appears.
  
  One cause is confirmed: the page loads before the test adds a camera, so it may open on Live instead of the Guide. Opening the Guide explicitly did not make it pass, so a re-render of the conversation after a site change is suspected. Not yet classified as a product or test defect.
- **Drone record recovery buffer.** In memory only: lost on a restart before the database recovers. Holds at most 200 records; retried every 5 s; audit entries are not replayed (`docs/drone-reliability.md` §6).
- **Wind estimate.** A real Lite reports no in-flight wind estimate until drag coefficients (`EK3_DRAG_*`) are measured for its airframe. Until then the operator's attested wind is the only wind check.
- **Untested on real hardware:**
  - SADP discovery against a real device;
  - Windows service installation;
  - push notifications to a phone;
  - the companion running on a Raspberry Pi;
  - live Claude API calls from the AI Guide;
  - planner geometry against a real site;
  - any physical aircraft, aerial footage or accessory.

## Phase 0: Pivot foundation (done, October 2026)
- [x] Audit the CameraAI codebase. Archive the senior-safety docs and keep that code frozen.
- [x] Rewrite `CLAUDE.md`, `AGENTS.md`, and `README.md`. Add architecture, API, and drone docs.
- [x] Multi-camera ingestion (RTSP/HTTP/USB/file) with reconnect, plus simulated cameras.
- [x] Object detection behind a pluggable interface (MediaPipe EfficientDet-Lite, Apache-2.0).
- [x] Tracking, zones with hysteresis, temporal rules (entry, dwell, repeated presence, sequence, multi-camera).
- [x] Conservative cross-camera correlation that refuses ambiguous identity merges.
- [x] Incidents, corroboration, alert policy (arm modes, rate limit, escalation), ntfy/MQTT notifiers.
- [x] SQLite event store, clip and snapshot recording, retention purge, audit log.
- [x] FastAPI `/api/v1` with token auth, SSE, and MJPEG. Dashboard with live, map, timeline, drone, and settings views.
- [x] Simulated drone, geofence, approval workflow, safety monitor. Telemetry-only MAVLink adapter.
- [x] Scenario replay plus 71 Sentinel tests.

## Phase 0.5: Real-world validation harness (done, October 2026)
- [x] `sentinel.bench`: recorded footage and live RTSP/HTTP/USB streams through the full pipeline, with detection, tracking, event, notification, latency, throughput, CPU, and memory metrics. See `docs/benchmarking.md`.
- [x] Public annotated footage fetcher (MOT16 day, night, and storefront; CAVIAR indoor lobby; an unlabeled ordinary-activity clip) and a manifest with zones per view.
- [x] Reference pass: annotated boxes and identities through the same rules give "expected events", so missed events and false alert events are measured, not guessed.
- [x] Simultaneous multi-camera sessions (3 and 6 feeds offline, 3 live RTSP via MediaMTX).
- [x] Synthetic stress conditions (low light, IR-like night, rain) applied to real footage and always labelled synthetic.
- [x] Playback/debug viewer (zones, annotations, detections, tracks, expected vs produced events) and annotated MP4 export.
- [x] Fixes driven by the findings: movement-aware dwell (`max_roam`), SQLite `synchronous=NORMAL` (engine p95 48 ms to under 0.5 ms on Windows), optional tiled detection, and a frame observer hook on `CameraWorker`. Sentinel tests now number 84.

## Phase 0.6: Detector upgrade (done, October 2026)
- [x] Licence screen of ONNX-capable detectors, checking code **and** weights licences plus training data (see `sources.md`). Shippable: YOLOX, RT-DETRv2, D-FINE (COCO-only weights). Benchmark only: RF-DETR (Objects365). Excluded: Ultralytics/AGPL, YOLOv6/7/9 (GPL), YOLO-NAS (proprietary weights).
- [x] `OnnxDetector` (`detectors_onnx.py`): YOLOX and DETR-family decoding, a model registry with licence metadata, configurable input size, thresholds, providers (DirectML/CUDA/OpenVINO/CPU), and CPU threads. Sessions are shared across cameras. EfficientDet is retained as the fallback.
- [x] YOLOX ONNX files run at any multiple-of-32 resolution through a one-time shape relaxation (weights untouched).
- [x] Hardware-aware default `model: auto`: YOLOX-s @960x544 on GPU, YOLOX-tiny @416 on CPU, EfficientDet-Lite0 if ONNX Runtime is missing.
- [x] Optional ByteTrack-style low-score track extension. New metrics: recall by visibility band and "group boxes" (merged people).
- [x] `min_box_area` lowered from 0.0015 to 0.0004. The old value discarded correctly detected distant people.
- [x] Comparison of 13 configurations with identical settings, full default benchmark, and live multi-camera RTSP tests. Sentinel tests now number 94 (168 including legacy).

## Phase 0.7: Setup, onboarding, and environmental intelligence (done, October 2026)
- [x] Camera onboarding wizard:
  - RTSP brand templates (Hikvision, Dahua/Amcrest/Lorex, Reolink, Uniview, Tapo, Axis) and DVR/NVR channels.
  - ONVIF WS-Discovery and profile reading.
  - HTTP snapshot/MJPEG fallback for recorders without RTSP.
  - USB webcams and analog capture devices, recorded footage, simulated cameras.
- [x] Connection testing:
  - RTSP-level diagnostics: DNS, unreachable, closed port, auth required/failed, wrong path, not RTSP.
  - Resolution, measured FPS, and codec, with a preview frame.
  - Main/sub stream testing and automatic sub-stream preference.
  - Hardware-decode detection (D3D11 verified: 1080p decode CPU from 35% to 12% of the machine) with software fallback.
  - Reconnect counts and error kinds in camera status.
- [x] Credentials: DPAPI-encrypted store. Never in config, API responses, or logs (tested, including with a fake camera and log capture). Stream addresses are write-only; unsaved tests are server-side drafts.
- [x] Visual setup editor in the dashboard:
  - Cameras: add, edit, delete, reorder.
  - Zones and privacy masks: draw, reshape, move, delete, and type on a live frame.
  - Property map: image upload, camera placement and aim, zone footprints.
  - Travel connections between cameras, and category settings.
  - Verified end to end in Microsoft Edge with Playwright.
- [x] Live configuration: validated, atomic saves with history. Edits reach the running engine immediately and restart only the affected camera. Rules that lose their zones are disabled, never widened.
- [x] Environmental intelligence:
  - COCO class audit and per-category `detect`/`alerts` filters, site-wide and per camera.
  - Specific labels in events ("Animal (dog)").
  - Scene conditions (lighting, IR, rain/snow/noise, shadows, recurring motion, near-lens objects) as info-only events.
  - `unknown_motion` proposals.
  - Missing categories and candidate datasets documented with licence constraints.
- [x] `sidewalk` zone type (public, never "on property"). Sentinel tests now number 125 (199 including legacy).

**Still requires real hardware:** onboarding against physical cameras/DVRs (only fake RTSP/ONVIF servers and MediaMTX so far), ONVIF discovery on a real LAN, USB/capture-card discovery, HTTP snapshot devices, hardware decoding of live RTSP, and the environmental thresholds on real rain, snow, insects, headlights, and IR switching.

## Phase 0.8: Network and camera diagnostics (done, October 2026)
- [x] `sentinel.netdiag`: read-only network intelligence on Windows (one PowerShell call), plus an `ip -j` Linux collector (unit-tested only):
  - adapters (Ethernet, Wi-Fi, VPN, virtual), addresses, routes, gateways, DHCP, network profile, firewall profiles;
  - findings for no connection, DHCP failure, no router, several networks, VPN carrying all traffic, and a Public profile blocking discovery.
- [x] Discovery, only after the person confirms they manage the network:
  - Hikvision SADP, which finds recorders with a wrong address range;
  - ONVIF sent out of every adapter;
  - optional port check capped at the local /24.

  Public addresses are refused everywhere.
- [x] Diagnosis of one address:
  - ICMP via `IcmpSendEcho`, so "host unreachable" is told apart from "no reply";
  - six TCP ports, the route taken, this computer's router and the recorder's gateway;
  - discovery, and brand recognition from ports and one HTTP request.

  Findings are confirmed / suspected / ruled out, each with evidence and brand-specific fix steps (Hikvision, Dahua/Amcrest, Lorex, Reolink, Uniview, generic), and Retest.
- [x] Recorder channel search over RTSP DESCRIBE. It stops on the first login failure, to avoid lockouts. Selected channels are added as cameras in one step.
- [x] Troubleshooter in Setup → Cameras, opened directly or from a failed wizard test. Verified in Edge with Playwright (`tests/test_sentinel_ui.py`).
- [x] Simulated networks (`SimulatedNetwork`, `config/netsim/`, `serve --simulate-network`). The Hikvision ERI-K108-P8 case (PC 10.0.0.57/24; recorder 192.168.1.84, gateway 192.168.1.254) is a regression test in five variants: SADP-confirmed, discovery blocked, recorder behind the provider's modem, VPN, and re-addressed.

**Real-network observation (the computer in the regression case):**
- 10.0.0.1 answers.
- 192.168.1.84 gives no ping reply and no answer on port 80.
- The Wi-Fi profile is Public.
- SADP and ONVIF found nothing.

So the cause is not yet confirmed. Next on site:
- switch the profile to Private and search again;
- or read the recorder's network menu on its monitor, set DHCP, and retest.

## Phase 0.9: 24/7 reliability (done, October 2026)
- [x] Honest coverage: one health state per camera (`monitoring`, `starting`, `offline`, `frozen`, `ai_failed`, `stalled`/`restarting`, `failed`). Monitoring only while changing frames reach a working detector. Live-tile overlay, header coverage badge, System tab, `/api/v1/system/health`.
- [x] Frozen-feed detection (pixel-identical samples), worker heartbeats, capture-to-decision and detection latency p50/p95, skipped-frame counts, CPU/memory/disk and NVIDIA GPU sampling.
- [x] Worker supervision: crash restarts with exponential backoff, stall detection with thread abandonment, a restart budget with a cooldown circuit breaker, a fatal exit when too many threads are stuck, manual restart.
- [x] Inference failures: GPU→CPU detector fallback; otherwise `ai_failed` until a reloaded detector succeeds on a real frame.
- [x] Outage history (SQLite `outages`), health timeline events that never open incidents, grouped and rate-limited coverage notifications plus recovery messages, notifications for Sentinel's own downtime (crash vs planned stop via `runstate.json`), and an optional external dead-man's-switch heartbeat.
- [x] Storage: free-space guard (oldest unprotected media first, then recording pause with hysteresis), media quota, protected recordings (open, marked, or `real_concern` incidents; 90-day retention), contained clip/snapshot/database write failures, rotating logs.
- [x] Backups: daily automatic and manual zips (config, settings, consistent SQLite copy, map image; credentials only on request), checksum and integrity verification, offline restore with a safety backup, live restore (config now, database at next start), damaged-database and damaged-config recovery at startup.
- [x] Background running: `sentinel run` process supervisor (backoff, budget, no retry on config errors), graceful stop requests, single-instance guard, Windows Task Scheduler install as the user (DPAPI), systemd unit.
- [x] Simulated faults (`disconnect`, `freeze`, `crash`, `stall`, `detector_error`), `scripts/reliability_smoke.py` (17 checks, about 35 s), and 61 new tests (292 in total, including legacy): unit, failure-scenario, real-process, and Edge UI.

**Not yet verified on real hardware:** multi-day runs on real cameras, frozen detection on real recorders (an on-screen clock keeps a frozen picture "changing"), a real full disk, and `service install` on a clean Windows machine.

## Phase 0.10: Security intelligence (done, October 2026)
- [x] Reliability corrections:
  - independent liveness signals (frames, decoder, stream clock, picture, inference heartbeat);
  - a noisy static scene stays monitoring; a frozen picture behind a ticking recorder clock is detected;
  - a still, noise-free picture is `unverified`, never `frozen`.
- [x] Evidence-preserving database recovery:
  - originals moved aside byte for byte with SHA-256;
  - rows salvaged from a copy and merged without overwriting the backup;
  - backup time, age and possible-loss window reported and notified;
  - offline and staged restores also preserve the replaced file.
- [x] Rule language:
  - Night and Vacation modes, day-of-week conditions, cooldowns (repeats recorded as information), per-rule notification preference;
  - minimum observations; arming later never fires an entry for someone already inside;
  - `combination` rules (several observations for one linked subject or anywhere).
- [x] Rule builder in Setup → Rules:
  - templates for residential and small-business situations, live validation, plain-language description;
  - a mandatory test of the current version before "Save and turn on";
  - tests run as a simulated walk, a scripted scenario, or recorded activity, through the real engine.
- [x] Explainable incidents: objects, cameras (observed or inferred), zones, sequence (observed vs inferred), rules with conditions, confidence and link uncertainty, severity reasons, evidence. Visual timeline with replay sync; a "Why" line in notifications.
- [x] Activity insights from logged visits and trips (no images, positions or identities):
  - patterns, changes over time, and explained unusual observations (information only);
  - suggestions that need the owner's approval;
  - no patterns until 7 days and 10 samples.
- [x] Investigation:
  - search, append-only notes, feedback labels;
  - nuisance and genuine rates per rule and camera, missed-event reports with Sentinel's own record of that time;
  - exports with SHA-256 evidence fingerprints.
- [x] New scenario `gate_then_shed.json`, 45 new tests (337 in total, including legacy): backend and Edge.

**Not yet validated:** rules, insights and nuisance rates on weeks of real household activity; content-monitor thresholds on real recorders and low-bitrate streams.

## Phase 0.11: Installation and hardware management (done, October 2026)
- [x] **Install tab:** one ten-step journey for four installation paths (existing cameras, new cameras, drone-first, complete package). Save and resume (`<storage>/installation.json`, included in backups), per-step issue counts, skip and revisit, completion that is refused while a blocking check fails and records acknowledged open issues, reopen and start over. Desktop and phone layouts.
- [x] **Honest readiness:** registered, connected, video available and AI monitoring verified are separate levels earned from live data. The strongest statement is "AI monitoring verified on N of M cameras"; Sentinel never says a property is protected. Simulated devices are always called out.
- [x] **Unified device registry** (`devices.py`): recorders, drones, docks and contact/motion/environmental sensors in `devices[]` beside `cameras[]`; capability lists; hardware compatibility profiles with support levels (verified, simulated, expected, limited, planned, unsupported). Cloud-only cameras cannot be registered. Planned hardware is never given an adapter, so it can never appear connected. Removal unlinks devices everywhere.
- [x] **Sites without cameras:** `cameras` may be empty; coverage reports `no_cameras`; the Live tab says there is no continuous video monitoring.
- [x] **Drones in the registry:** several drones (the primary takes inspection requests), registration from the installer with a property-rectangle geofence, inspection locations requested from the Drone tab and map, rebuilds deferred while a drone is flying. Flight commands for real aircraft stay disabled.
- [x] **Property planner** (`planner.py`): buildings, driveways, gates, fences, public and privacy areas, rooms, cameras with aim and field of view, sensors, network equipment, drone docks and points. Approximate coverage with line of sight, door approaches, gaps and privacy warnings; a detection-range estimate from the benchmark (a person must fill about 10% of the picture height); camera suggestions from a questionnaire; PoE ports, budget and cable runs; printable installation plan with checklists. Every result states its assumptions.
- [x] **Notifications from the installer:** ntfy topic kept in the credential store, channels applied without a restart, and a test notification with per-channel results.
- [x] **Troubleshooting:** the existing troubleshooter is reachable from every camera path, from unverified cameras and from recorders. New gap filled: guidance for new PoE, Wi-Fi and login/codec problems.
- [x] 34 new tests: backend, service (including simulated cameras earning "AI monitoring verified" from real frames, a fake ntfy server, backups) and Edge.

**Not yet verified:** any physical camera, recorder, PoE switch, Wi-Fi camera or sensor through the installer; planner coverage against a real installation; a test notification on a real phone; the installer used by a non-technical person.

**Remaining hardware integration requirements:**
- Recorder channels found on the real ERI-K108-P8 (blocked on its network address; see Phase 0.8).
- Real sensors: MQTT / Home Assistant ingestion, then sensors as rule inputs (Phase 4). Sensors are registration-only or simulated today.
- Drone video ingested as a camera source, a dock adapter, and the gated MAVLink path (Phase 5; handled in the separate drone sessions).
- Indoor drones need positioning without GPS, which the geofence does not model.
- A Sentinel Hub hardware profile once hardware is chosen.

## Phase 0.12: Sentinel AI Guide (done, October 2026)
- [x] Guide tab, opened first while installation is unfinished: welcome, five conversational starting points, saved conversations (on the hub) with resume and deletion, desktop and phone layouts.
- [x] Fixed tool set over existing services: installation progress and readiness, devices, camera health, hardware profiles, installation guidance, plan coverage, camera suggestions, network overview, diagnosis of one private address, camera connection tests, rules and zones, rule validation and testing, incidents and explanations, insights, system health, drone status, notification settings.
- [x] Changes only as proposals the person confirms (installation type and steps, network search, rules, rule switches, notifications, device registration and removal, drone inspection requests); applied through the existing validated backend, audited, results reported from the backend. Rules can only be turned on after a test of the same version.
- [x] Camera onboarding in conversation: permission before searching, one next step per diagnosis with confirmed/possible causes kept apart, Retest, secure sign-in form for recorder channels (passwords never enter the conversation), add selected cameras, then verify.
- [x] Built-in offline guide over the same tools; optional Claude provider (`assistant.provider: "anthropic"`, key from the environment or credential store) with address placeholders, prompt caching, refusal fallback, bounded tool rounds, and fallback to the guide on any failure.
- [x] 27 new tests (backend with a scripted provider and a fake Messages API, and Edge). Two defects found by them were fixed: addresses ending a sentence escaped the placeholders, and a confirmed cause was labelled as unconfirmed.

- [x] DeepSeek provider (default external choice): `deepseek-flash` (default) or `deepseek-v4-pro` over DeepSeek's Chat Completions API, with tool calls, several calls per round, `reasoning_content` replay, error fallback and the same protections. Key from `DEEPSEEK_API_KEY` or the credential store. Checked against the live API on a simulated site.

**Not yet verified:** the live Claude API (only a fake API was used), real hardware through the guide, and use by non-technical customers.

## Detector upgrade findings (2026-10-13)

Host: i7-2600K (4 cores, no AVX2), 16 GB RAM, GTX 1070 via DirectML (ONNX Runtime 1.24). Every number comes from human annotations (MOTChallenge MOT16, CAVIAR). Thresholds are each model's operating point from a score sweep on sampled frames **of the same footage**: a single scalar per model, but not an independent test. Raw tables: `bench_runs/detectors_v2/comparison.md` and `bench_runs/final_default/report.md` (git-ignored; regenerate with `scripts/bench_detectors.sh` and `sentinel.bench run --all`).

### Comparison (same 5 clips, full pipeline, `min_box_area` 0.0004)
| Configuration | Provider | MOT16-09 P / R (≥50% visible) | MOT16-02 P / R (≥50% visible) | MOT16-04 P / R | CAVIAR P / R | Mostly tracked | Alert events found | False notifications | Detect p50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EfficientDet-Lite0 (old default) | CPU | 99% / 28% (44%) | 92% / 8% (18%) | 98% / 2% | 87% / 10% | 0/165 | 1/44 | 0 | 104 ms |
| EfficientDet-Lite2 | CPU | 99% / 30% (48%) | 87% / 9% (22%) | 90% / 6% | 45% / 17% | 2/165 | 4/44 | 1 | 293 ms |
| YOLOX-tiny @640x384 | GPU | 91% / 71% (95%) | 94% / 27% (57%) | 98% / 43% | 68% / 14% | 25/165 | 12/44 | 1 | 28 ms |
| YOLOX-s @640 | GPU | 97% / 71% (95%) | 92% / 25% (53%) | 98% / 36% | 79% / 12% | 26/165 | 7/44 | 1 | 27 ms |
| **YOLOX-s @960x544 (default, GPU)** | GPU | **95% / 74% (97%)** | **87% / 32% (66%)** | **98% / 53%** | 87% / 15% | 34/165 | 11/44 | **0** | **42 ms** |
| YOLOX-s @960 + low-score tracking | GPU | 95% / 74% (97%) | 87% / 32% (66%) | 98% / 53% | 89% / 16% | 41/165 | 14/44 | 2 | 49 ms |
| YOLOX-m @960x544 | GPU | 92% / 76% (99%) | 95% / 35% (70%) | 94% / 56% | 85% / 20% | 42/165 | 14/44 | 1 | 60 ms |
| YOLOX-m @1280x736 | GPU | 90% / 76% (98%) | 95% / 39% (74%) | 95% / 62% | 98% / 20% | 43/165 | 14/44 | 1 | 96 ms |
| YOLOX-s, 2x2 tiles | GPU | 73% / 67% (93%) | 91% / 33% (67%) | 96% / 57% | 94% / 22% | 40/165 | 15/44 | 1 | 128 ms |
| RT-DETRv2-R18 @640 | GPU | 90% / 74% (97%) | 96% / 30% (63%) | 99% / 37% | 62% / 24% | 27/165 | 11/44 | 1 | 64 ms |
| D-FINE-s @640 | CPU only | 92% / 72% (96%) | 89% / 30% (60%) | 98% / 35% | 81% / 18% | 29/165 | 8/44 | 1 | 540 ms |
| RF-DETR base @560 (licence review) | GPU | 95% / 75% (98%) | 93% / 27% (57%) | 99% / 29% | 86% / 35% | 27/165 | 8/44 | 1 | 310 ms |

CPU-only latency (i7-2600K, one 1080p frame): YOLOX-nano @416 38 ms, **YOLOX-tiny @416 94 ms (CPU default)**, YOLOX-tiny @640x384 138 ms, YOLOX-s @640 386 ms, YOLOX-s @960 486 ms. EfficientDet-Lite0 is 97 ms. On sampled frames, YOLOX-tiny @416 reached 98% precision and 63% recall on MOT16-09, against 26% for Lite0 at the same cost.

### Targets
| Target | Result | Met? |
| --- | --- | --- |
| ≥70% person recall on MOT16-09 | 74% at 95% precision (default). Every YOLOX/DETR configuration without tiling reached 71–76% (2x2 tiles: 67%) | **Yes** |
| ≥50% recall on MOT16-02 | 32% (default) and 39% (YOLOX-m @1280). For people at least 50% visible: 66% and 74% | **No** |
| ≥90% precision where feasible | 95% (MOT16-09) and 98% (MOT16-04) with the default. 87% on MOT16-02 (95% with YOLOX-m). CAVIAR 87% | Mostly |
| 5 processed fps per camera | 4.5–4.9 fps live (motion-heavy crowds, so detection runs every frame) | Close; below by up to 10–18% under load |
| Graceful multi-camera degradation | 6 cameras: 4.5 fps average (minimum 4.1), p95 187 ms, 13.5% CPU | **Yes** (GPU) |

### Full event pipeline, old vs new default (8 labeled clips)
| | EfficientDet-Lite0 | YOLOX-s @960 |
| --- | --- | --- |
| Alert-level events found (annotation reference) | 1 of 50 | 12 of 50 |
| Restricted-zone entries / dwell / repeated visits | 0/14, 0/33, 1/3 | 5/14, 4/33, 3/3 |
| First-sighting (activity) recall | 32 of 176 | 109 of 176 |
| Notifications matched | 2 of 8 | 4 of 8 |
| False notifications (labeled clips) | 0 | 0 |
| False alert events | 2 | 6 |
| Identities mostly tracked (5 clips) | 0 of 165 | 34 of 165 |

### Why MOT16-02 still falls short: small, occluded, and merged people
- **Occlusion:** recall by visibility band (≥75% / 50–75% / 25–50% / <25%) is 72% / 48% / 25% / 5% on MOT16-02, and 98% / 94% / 80% / 25% on MOT16-09. MOT16-02 is a dense plaza in which many annotated people are mostly hidden behind others. The detector finds clearly visible people well.
- **Distance:** small people (<10% of frame height) are found 17–18% of the time on MOT16-02 and MOT16-04, against 82–100% for large ones. Raising input resolution is the best lever (MOT16-04 recall: YOLOX-s @640 36%, YOLOX-s @960 53%, YOLOX-m @1280 62%). Tiling reaches similar recall but costs precision (73% on MOT16-09).
- **Merging:** "group boxes", one false box covering two or more people, are 17% of MOT16-02's false positives with the default (24 of 144). Tiling quadruples them (84 across 5 clips).
- **Tracking:** identity switches rise with recall (Lite0 47, default 178 on 5 clips). Low-score track extension (ByteTrack-style) helps continuity and event recall (YOLOX-m: mostly tracked 42→45, switches 203→179, alert events 14→16) but added false notifications. It stays opt-in.
- **Overhead fisheye (CAVIAR):** all models are weak (12–35% recall). COCO-trained detectors rarely see top-down people. RF-DETR is best here but is not shippable yet.

### Live multi-camera load (RTSP via MediaMTX, 60 s each, crowded streams = worst case)
| Setup | fps per camera (target 5) | p95 capture-to-decision | CPU (share of 8 threads) | Peak RSS |
| --- | --- | --- | --- | --- |
| Lite0, 3 x 1080p30 (before) | 2.8–2.9 | 390–415 ms | 65% | 755 MB |
| **Default (GPU), 1 x 1080p30** | 4.9 | 121 ms | 12% | 543 MB |
| **Default (GPU), 3 x 1080p30** | 4.6–4.7 | 197 ms | 55% (mostly decoding) | 812 MB |
| Default (GPU), 3 x 640p10 sub-stream | 4.5–4.7 | 165 ms | 8% | 482 MB |
| **Default (GPU), 6 x 640p10 sub-stream** | 4.1–4.5 | 187 ms | 13.5% | 584 MB |
| CPU profile (YOLOX-tiny @416), 3 x 640p10 | 3.8–4.0 | 419 ms | 56% | 252 MB |
| Lite0, 3 x 640p10 (reference) | 4.5–4.7 | 228 ms | 19% | 381 MB |

### Selected defaults
- **GPU host:** `{"type": "onnx", "model": "auto"}` resolves to YOLOX-s @960x544 with threshold 0.5. That is about 23 inferences/s on a GTX 1070, enough for 4 continuously active cameras at 5 fps, or more with idle scenes (the motion gate).
- **Small installations (1–2 cameras, GPU), best accuracy:** YOLOX-m @1280x736, threshold 0.5.
- **CPU-only host:** YOLOX-tiny @416, threshold 0.45. Plan for 3 sub-stream cameras on a 4-core desktop-class CPU.
- **Streams:** use camera sub-streams (640p, 10 fps) unless a GPU decoder is added. 1080p decoding dominates CPU.

### Bottlenecks and remaining limitations
- **Decode, not inference, is now the CPU bottleneck** at 1080p (55% of the machine for 3 streams). Hardware video decode (NVDEC/D3D11VA via FFmpeg, or GStreamer) is the next performance step.
- Crowded scenes and heavy occlusion: the MOT16-02 target is unmet. Options, in order: YOLOX-m/l at 1280 on stronger GPUs, a crowd-trained detector (license-permitting; CrowdHuman is non-commercial, so it is not usable), and better tracking.
- D-FINE gives wrong results on DirectML (verified): CPU only. RF-DETR is slow on DirectML (310 ms) and awaits legal review.
- Thresholds were chosen on the same public footage. Re-tune on labeled footage from the target cameras before quoting accuracy to customers.
- GPU results are for one GTX 1070 via DirectML. CUDA, OpenVINO, and NPU providers are untested.
- All public footage is urban or indoor and crowded, not residential. Real IR night footage is still untested.

## Real-footage benchmark findings (2026-10-13, EfficientDet-Lite0 baseline; superseded by the detector upgrade above)

Host: Windows 10, 8 logical CPUs, CPU only. Detector: MediaPipe EfficientDet-Lite0 (default) unless stated. Reports: `bench_runs/final_lite0/report.md` (git-ignored; regenerate with `python -m sentinel.bench run --all`). Every accuracy number below comes from human box annotations (MOTChallenge MOT16, CAVIAR). Nothing is claimed for unlabeled footage.

### 1. Detection recall is far too low for unattended alerting
| Clip (annotations) | Precision | Recall (all / at least 50% visible) | Recall small / medium / large |
| --- | --- | --- | --- |
| MOT16-09 storefront, doorbell height, day | 99% | 28% / 44% | n/a / 17% / 36% |
| MOT16-02 plaza, eye level, day, crowded | 92% | 8% / 18% | 0% / 5% / 37% |
| MOT16-04 elevated street, night (brightly lit), crowded | 98% | 2% / 3% | 0.3% / 2% / 25% |
| CAVIAR lobby, overhead fisheye, 384x288 (5 clips) | 60-100% | 7-16% | about 1% / 26-47% / 50% |

- Precision is high: when Sentinel reports a person, it is almost always right. But it misses most people, especially anyone under about 25% of the frame height. Groups walking together are often merged into one box.
- MOT16-04 is lit by street lamps, so its very low recall is a **scale** problem (distant people in a 1080p frame downscaled to the model's 320 px input), not a darkness problem. **There is no evidence yet about real IR or night-vision cameras.**
- Overhead fisheye views (CAVIAR) are poorly served by a COCO-trained detector.

### 2. Bigger models and tiling help, but not enough, and cost CPU
| Detector | Recall (MOT16-02 / -04 / -09) | Precision (same) | Detect p50 per frame |
| --- | --- | --- | --- |
| Lite0 (default) | 8% / 2% / 28% | 92% / 98% / 99% | about 105 ms |
| Lite2 | 9% / 6% / 30% | 87% / 90% / 99% | about 300 ms |
| Lite0 + 2x2 tiles | 13% / 6% / 46% (69% of visible people) | 75% / 79% / 72% | about 500 ms |

Lite2 cut precision on CAVIAR to 42-54%. Tiling adds false boxes, mostly one box around a group plus the individuals, and more identity switches. **Decision: a stronger detector is the top Phase 1 item.** Candidates are a permissively licensed ONNX model (YOLOX or RT-DETR, Apache-2.0) at 640 px input, on an NPU or GPU where available. Measure each with `sentinel.bench --model` against these numbers.

### 3. Events and alerts follow detection
- Labeled clips (8 single-clip runs), against the annotation reference: alert-level events found 1 of 50 expected. Restricted-zone entries: 0 of 14. Dwell: 0 of 33 (mostly people waiting on the night street). Repeated visits: 1 of 3. Activity (first sighting) recall: 12-40% per clip (32 of 176 overall).
- False alerts on labeled clips: **0 false notifications**. There were 2 false alert events, both restricted-doorway entries on MOT16-02 from boxes the annotations don't support. Notifications matched: 2 of 8 expected.
- Unlabeled ordinary-activity clip (`vtest`): before the fix, 4 "remained in Walkway 30 s" alerts. Investigation showed tracks walking 11-22 body heights in loops (and likely identity switches) with small net displacement. Replacing endpoint displacement with a positional extent (`max_roam`) reduced this to 1 alert, which is **unverified**: the track sits in a busy crossing, and without box labels we cannot say whether it is a real lingerer or an identity switch. The rule change was motivated by this clip, so it is not an independent test of the rule.
- Tracking: no annotated identity was "mostly tracked" on any clip (a consequence of low recall). Identity switches rise as recall rises (MOT16-09: 26 with Lite0, 60 with tiles), so the IoU tracker becomes the next bottleneck after detection.

### 4. Performance and multi-camera load
| Setup | Per-camera fps (target 5) | Capture-to-decision p95 | CPU (share of 8 threads) | Peak memory |
| --- | --- | --- | --- | --- |
| Live, 3 x RTSP 1080p30 | 2.8-2.9 | 390-415 ms | 65% | 755 MB |
| Live, 3 x RTSP 640x360 at 10 fps (camera sub-stream) | 4.6-4.9 | 219-229 ms | 21% | 378 MB |
| Offline, 3 x 1080p, one thread | (0.81x real time) | n/a | 25% | 633 MB |
| Offline, 6 mixed feeds, one thread | (0.79x real time) | n/a | 18% | 814 MB |

- Full-rate 1080p decoding plus detection saturates the CPU at three cameras. Under contention, detector time rises from about 105 to about 260 ms. **Use camera sub-streams.**
- The sub-stream costs some recall: MOT16-02 8% to 7%, MOT16-04 2% to 1%, MOT16-09 28% to 23%. With a stronger detector this trade-off must be re-measured.
- After the SQLite fix, the engine, incidents, and storage take under 0.5 ms p95 per frame. Brain logic is not a performance concern.
- The RTSP path (reconnect backoff, offline reporting) was exercised against MediaMTX, including the camera-unreachable case. **No physical IP camera or USB webcam has been tested.**

### 5. Synthetic conditions (degraded real daytime footage, not real recordings)
- Synthetic low light: recall MOT16-02 8% to 2%, CAVIAR 10% to 3%, MOT16-09 28% to 21%.
- Synthetic rain: 8% to 2%, 10% to 7%, 28% to 19%.
- Synthetic IR-like grey: roughly unchanged on MOT16 (7% and 24%), 10% to 2% on CAVIAR.
- These only show direction: degraded imagery makes an already weak detector weaker. Real night/IR and rain recordings with labels are needed.

### Limitations of this evaluation
- Public footage is urban and crowded (dozens of people) or an indoor lobby. That is not residential: typical homes have few, closer subjects, which should favour recall. This cannot be assumed; it must be measured on labeled footage from target cameras.
- Event ground truth is derived from annotations run through our own zones and rules (the reference pass); there are no hand-written expected events yet. It measures perception's effect on events, not whether the rules themselves are right. Event matching ignores identity.
- Zones and short dwell thresholds were drawn for these clips. Clips are 17-80 s long, so long-dwell and repeated-visit behaviour is barely exercised.
- Cross-camera correlation is untested on real footage: the clips are from unrelated cameras. A multi-view dataset (for example PETS 2009, EPFL multi-camera, or own cameras) is needed.
- Licences (MOT16 CC BY-NC-SA, CAVIAR research use) allow evaluation only. Never train shipped models on them.
- Offline sessions run single-threaded, so live mode is the source of truth for multi-camera capacity.

## Phase 1: Brain on real cameras (next)
Exit: 2+ real IP cameras running for 7 days with useful incidents and fewer than 1 nuisance push per day.
- [x] **Detector upgrade:** done (see Phase 0.6). MOT16-09 target met; MOT16-02 not met (32%; 66% of visible people).
- [x] Hardware video decoding via OpenCV/FFmpeg (`hw_decode: auto`, D3D11 on Windows) with software fallback. Still to measure: live multi-camera RTSP with hardware decode.
- [ ] Validate environmental thresholds and unknown-motion on labeled real footage (rain, snow, insects at night, headlights, IR switching); tune the `environment` settings.
- [ ] Package/parcel detection: evaluate an additional detector trained on licence-cleared data (Open Images V7 *Box*/*Envelope*, own footage); see docs/environmental-intelligence.md.
- [ ] Crowd and occlusion work: evaluate YOLOX-l and larger inputs on capable GPUs, and track-level re-detection. Measure on MOT16-02 and own footage.
- [ ] Overhead/fisheye views: evaluate dewarping before detection, or avoid ceiling-mounted fisheye cameras in installation guidance.
- [ ] Legal review of RF-DETR (Objects365 pre-training) and of COCO image provenance for commercial distribution.
- [ ] Record and label 1-2 hours from the target residential cameras (day, night/IR, rain, deliveries, ordinary family activity). Box labels via CVAT (MOT export), plus hand-written expected events and `expect_no_notifications` for ordinary footage. This replaces public footage as the acceptance set.
- [ ] Resolve the ERI-K108-P8 install with the troubleshooter (see Phase 0.8) and record what the real SADP reply and channel search returned.
- [ ] Connect 2-3 real RTSP cameras on sub-streams (640p, 5-10 fps; measured 21% CPU for 3 cameras on this host). Run `sentinel.bench live` for 24 h and record latency, dropped frames, and reconnects. Run under `sentinel run` for 7 days and review the outage history and every health notification.
- [ ] Tracker upgrade (ByteTrack or OC-SORT style, using low-score detections) once recall improves. Measure identity switches and "mostly tracked" before and after.
- [ ] Group-box handling: the detector often emits one box for two people walking together. Evaluate whether the stronger detector removes this before adding logic.
- [ ] Multi-view dataset for cross-camera correlation validation (own cameras with overlapping or adjacent views).
- [x] Zone editor in the dashboard (Phase 0.7).
- [ ] Calibrate topology transit times from observed walks. Add a tool that suggests `min/max_transit_s` from stored site tracks.
- [x] Detector benchmark harness and first comparison (Lite0, Lite2, and tiles on public footage; see findings). Still needed: real IR/night and rain recordings, and vehicle-class labels.
- [x] Label review loop: feedback labels and nuisance rate per rule and camera (Phase 0.10). Still to do: collect a month of real feedback.
- [ ] Clip playback: H.264 encoding via a bundled ffmpeg or PyAV fallback when OpenCV lacks `avc1`.
- [ ] Night-specific handling: IR mode switch, headlight glare suppression, spider-web and insect false boxes (minimum track displacement or persistence).
- [ ] Event deduplication when two overlapping cameras see the same person (`overlap: true` links tested on real views).

## Phase 2: Pilot hardening
Exit: a non-technical household runs it for 30 days without help.
- [x] Service install (Windows Task Scheduler / systemd), auto-start, watchdog, log rotation, disk-space guard (Phase 0.9). Still to do: install and run it for 30 days on the pilot machine.
- [ ] HTTPS via a reverse proxy recipe (Caddy) and a VPN recipe (Tailscale/WireGuard).
- [ ] User accounts with roles (owner, member, viewer), per-user tokens, session expiry. Replace the single shared token.
- [ ] Secrets: OS keyring or `.env` loaded by the service manager. Document rotation.
- [x] Backup and restore of config, settings, and the SQLite store (Phase 0.9). Still to do: off-machine backup copies (encrypted, user-chosen location).
- [x] Health page: per-camera state, detector latency, skipped frames, storage use, outage history, and an external watchdog heartbeat (Phase 0.9). Still to do: uptime percentages per camera over 7/30 days.
- [ ] Schedules: automatic arm and disarm by time, and geofenced presence from phones (Phase 3 app).
- [ ] Self-hosted ntfy or push relay. Notification action buttons (acknowledge, expected activity).

## Phase 3: Mobile app
Exit: an iOS/Android app with push, live view, timeline, and arm/disarm.
- [ ] React Native/Expo app against `/api/v1`. Phone registration endpoint (`/api/v1/push-clients`; `/api/v1/devices` is the hardware registry) and APNs/FCM relay.
- [ ] Secure remote connectivity without port-forwarding: VPN-based first, then an optional relay service.
- [ ] Live view over WebRTC (go2rtc or MediaMTX alongside Sentinel) instead of MJPEG for bandwidth.

## Phase 4: Intelligence upgrades (only with data)
Exit: measured improvement on a held-out set of real household incidents.
- [ ] Stronger tracker (ByteTrack/OC-SORT) if identity switches show up in reviews.
- [ ] Appearance re-identification embeddings (an OSNet-class model with a permissive licence) as a stronger cross-camera tie-breaker. Still coarse, still never an identity.
- [ ] Learned per-site baselines: typical arrival hours, delivery patterns, and dwell distributions, so unusual-for-this-property scoring layers on top of the rules.
- [ ] Package delivered and removed detection (object left or taken in an entrance zone).
- [ ] Optional integrations: door and window contacts, smart locks, and lights over MQTT/Home Assistant as extra rule inputs.

## Phase 5: Drone (gated, see `docs/drone-integration.md`)
Exit for each step is listed in the drone doc. Never skip steps. The drone track runs alongside Phase 1 and must not delay it.

### Phase 5.0: SITL integration and V1 architecture (done, 2026-10-14; no hardware bought, no physical flight)
- [x] Hardware architecture, re-evaluated without a price target (`docs/drone-hardware-v1.md`):
  - **recommended:** Holybro X650, Pixhawk 6X on the Pixhawk RPi CM4 baseboard (ArduCopter 4.7), SIYI ZT6 (thermal + 4K), SIYI HM30, ExpressLRS, dual M10, LightWare SF45/B;
  - day variant with the A8 mini;
  - alternatives: X500 V2, Herelink, ModalAI, enterprise docks;
  - weight/power budget, wiring, bench procedure, BOM (about $7.2–8.5k full, $4.3–5.1k day, $1.0–1.4k bench rig), product-readiness gaps, Canadian scenarios and blockers.
- [x] ArduPilot SITL telemetry through `MavlinkTelemetryAdapter`:
  - real ArduCopter 4.7.2 in WSL2 (`scripts/sitl/start_sitl.sh`);
  - reconnect after EOF, Hub ground-station heartbeat, stream requests;
  - 1,369 parameters in about 3 s, read-only fence download, gimbal attitude, pre-arm and EKF state.
- [x] Read-only parameter audit:
  - failsafes, fence type/action/ceiling, RTL altitude, arming checks;
  - the FC polygon inside Sentinel's geofence;
  - changes since recorded commissioning;
  - ArduCopter 4.7 renames handled.

  It blocks approval; SITL defaults correctly failed it until commissioning.
- [x] Propose mode for real aircraft:
  - approval exports QGroundControl `.plan` / `.waypoints` (vertical climb to the safe transit altitude, transit, hover, RTL; every leg checked against the geofence);
  - the pilot flies it, and Sentinel follows the phases from telemetry.

  Flown in SITL by a ground-station stand-in.
- [x] Command mode for simulators only (`SitlMavlinkAdapter`): requires `allow_flight_commands`, a loopback link and SIM_* parameters. Physical aircraft refuse every command.
- [x] Readiness (15 checks, including operator-confirmed weather and conditions), operator advisories instead of commands for real aircraft, flight log, maintenance and commissioning records, `drone_link` health component, restart recovery (an inspection in flight is marked interrupted, never resumed).
- [x] Companion computer software (`sentinel.companion`):
  - MAVLink relay with a CRC-checked Hub→FC allowlist;
  - health agent;
  - one-time pairing codes and HMAC sessions (secret in the credential store);
  - Pi provisioning (MediaMTX, systemd).
- [x] Drone video:
  - analysed on the Hub only during inspections;
  - tracked across frames;
  - ground position with stated uncertainty (or the reason none is possible);
  - fingerprinted snapshots;
  - observations reported beside the fixed cameras in the incident explanation, never merged.
- [x] Failsafe tests in SITL: link loss → the FC's own GCS failsafe returned home; low battery → return home.
- [x] End-to-end demo (`scripts/drone_sitl_demo.py`), through the real service and API:
  1. A simulated camera's high incident ("Person entered Shed") created a request.
  2. The companion paired, commissioning was recorded, and an operator approved.
  3. The pilot stand-in flew the exported plan (25 s, 60 m).
  4. The simulated drone camera found a person in 23 of 125 frames, 1.4 m (±3.3 m) from the inspection point (scripted truth about 1.1–1.7 m).
  5. A fingerprinted snapshot was attached to the incident.
- [x] Tests: 44 new (unit, interface, API, Edge) plus 6 SITL integration tests (`SENTINEL_SITL=1`). 448 in total pass (6 SITL skipped without a simulator); `reliability_smoke.py` 17/17.

**Still simulated:** aircraft physics, both cameras, the scripted drone detector, the pilot, the companion (ran on the Hub, not a Pi), and the radio link (loopback UDP). No real gimbal attitude, video latency, link range, endurance or wind has been measured.

### Phase 5.0b: drone models, cheapest drone first (done, 2026-10-14; no hardware bought, no physical flight)
- [x] Three reference designs sharing flight stack, radio, camera protocol and software (`docs/drone-models.md`):
  - **Sentinel Lite**, the engineering priority: X500 V2 PX4 dev kit (Pixhawk 6C, M10), SIYI A8 mini, HM30, ExpressLRS, **no companion computer**; about $1.3–1.8k in parts (est.);
  - **Sentinel Pro**: X650, Pixhawk 6X + CM4, ZR10 zoom, dual GPS, optional lidar; about $3.1–4.2k;
  - **Sentinel Thermal**: the V1 full configuration (ZT6, lidar); about $6.4–8.0k.
  - Consumer drones (DJI MSDK runs only in an Android app on the controller) are not claimed as supported.
- [x] Finding (SITL, ArduCopter 4.7.2): `MAV_OPTIONS` bit 0 with `MAV_GCS_SYSID` set to the pilot makes the flight controller ignore every Hub command. That is the Lite's independent command lockout without a companion. It also drops the Hub's stream and parameter requests, so:
  - commissioning sets stream rates on the radio port;
  - Sentinel audits the parameters and fence it overhears when the pilot's ground station downloads them.

  **4.7 numbers MAVLink stream groups from 1:** TELEM1/SERIAL1 is `MAV2_*` (`MAV1_*` produced nothing on SERIAL1); 4.6 uses `SR1_*`.
- [x] Model capability system (`drone/catalog.py`):
  - registration with `drone_model`, options and a write-only stream address;
  - model-specific readiness: lockout, companion, camera stream, daylight/lights, wind and temperature limits, model FC settings;
  - route endurance check at approval;
  - camera geometry and simulator performance per model;
  - troubleshooting with model-specific steps (`drone/troubleshoot.py`);
  - a comparison screen (Install and Drone tabs);
  - AI Guide tools `get_drone_models` and `diagnose_drone`, and model-aware registration.
- [x] Fixed on the way: drone video stream addresses were returned by `GET /config` (now write-only); a drone registered without a position sat on the geofence corner and failed readiness (now the middle of the property); Sentinel stayed silent when the FC's own failsafe was already returning (now it says why).
- [x] Tests:
  - per-model simulated inspections in the built-in simulator (incident → approval → flight → video → evidence; link loss; low battery; night refused for Lite, flown by Thermal);
  - **Lite in SITL** with its own commissioning (`SENTINEL_SITL=lite`, 4/4): lockout and passive audit, pilot-flown inspection with video and evidence, radio loss → FC return home, low battery → FC failsafe plus advisory;
  - **Pro/Thermal profiles in the V1 SITL suite** (`SENTINEL_SITL=1`, 7/7).
- [ ] Measure everything the catalog estimates (hover time, wind limit, mass, ZR10/ZT6 field of view, A8 mini RTSP address) on hardware.

### Phase 5.0c: reliability, Lite cost-down and hardware readiness (done, 2026-10-15; no hardware bought, no physical flight)
- [x] Engineering audit of the drone stack (findings and fixes in `docs/drone-reliability.md`). Fixed:
  - stale or implausible telemetry treated as current: (0,0) positions, battery > 100 %, no per-signal ages, a gimbal angle used after the gimbal stopped reporting;
  - no altitude check in the in-flight geofence monitor, and the flight controller's own fence and battery verdicts ignored;
  - "returning home" advised while navigation had failed;
  - refused safety commands retried every tick with unbounded notes (simulators);
  - a database outage stopped the drone safety loop;
  - an expired request approvable between ticks;
  - an inspection whose aircraft was never heard again blocked all later approvals;
  - maintenance records in the same millisecond overwrote each other;
  - frozen or absent drone video reported as "analysed … did not detect anything";
  - the companion relay forwarded anything sent to its ground-station socket by any device on the network;
  - the provisioning default sent the pilot's telemetry to the Pi itself;
  - FC restarts not detected (stale settings audited); ACKs meant for the pilot taken as the Hub's on a shared radio; overlapping fence downloads.
- [x] Findings from ArduPilot source and SITL:
  - `MAV_OPTIONS` trusts a system id (a compromised device can claim 255) and ArduPilot routes packets to other MAVLink devices before that check;
  - MAVLink signing rejects unsigned packets on every port but USB, and is now recognised as a lockout;
  - ArduCopter keeps `REQUEST_DATA_STREAM` rates in RAM (ArduPlane would save them, so the request is sent to copters only);
  - with voltage-only battery thresholds the FC kept flying while its capacity count was near empty: `BATT_LOW_MAH` recommended (non-blocking audit item).
- [x] Flight-health monitor, staleness-aware readiness, the FC's battery/fence verdicts, navigation-loss advisories, lost-contact timeout, simulator restart recovery.
- [x] Fault-injection framework (`drone/faults.py`) and SITL failure scenarios (`scripts/sitl/fault_scenarios.py`) on a separate SITL instance with a simulated SIYI gimbal. Results (Sentinel Lite parameters, ArduCopter 4.7.2, latest run of each, `bench_runs/recovery_20261015/` and `bench_runs/v1freeze_20261015/`, details in `docs/drone-reliability.md` §6): 45 of 48 scenarios pass. 3 are not modelled by design and remain unvalidated hardware bench items: `companion_power_loss`, `fc_restart_in_flight` and `camera_latency`.
- [x] Propeller-free hardware bench tool (`sentinel.drone.bench`) with a read-only command-path probe, acceptance criteria (`docs/drone-bench-testing.md`), and the setup journey with evidence-based levels (Drone tab, AI Guide `get_drone_setup`, `check_drone_camera`).
- [x] Lite cost-down study (`docs/drone-lite-cost.md`):
  - F405 boards ruled out (no SIYI driver in their official builds);
  - the cheapest acceptable Lite is about $1.19–1.56k (S500 V2, MicoAir743, one pack);
  - the prototype stays on the X500 V2 dev kit (about $1.41–1.85k at today's prices);
  - Lite endurance estimate lowered to 13 min.

### Phase 5.0d: optional accessories and FLAME Mode (done in software, 2026-10-15; nothing built, no ignition hardware)
- [x] Removable accessories on the existing aircraft (`drone/accessories.py`):
  - registry category `accessory`;
  - compatibility records (mass, endurance, sensors, interface, CG, heat);
  - commissioning with measured mass;
  - inspections and simulated health faults.
  Ignition payloads are evaluation records only.
- [x] Security ↔ agricultural monitoring configuration:
  - switched only on the ground;
  - each configuration refuses the other's requests;
  - endurance adjusted for the fitted accessory.
- [x] FLAME Mode (`flame/`):
  - fields, watch strips and sensitive places;
  - checklist, crew, equipment, permits and qualifications (recorded, not verified);
  - weather against the burn boss's limits, and smoke direction;
  - simulated fire and thermal watch, with coverage and position uncertainty;
  - heat outside, at and near the boundary;
  - drone and accessory faults reported to people;
  - post-burn inspection and fingerprinted reports;
  - FLAME tab and AI Guide tools.
- [x] Validation:
  - unit, API, Guide and Edge tests;
  - SITL scenario on ArduCopter 4.7.2 with the Thermal parameters and a simulated SF45/B (`tests/test_sentinel_flame_sitl.py`, passed 2026-10-15). It covers registration, configuration switch, field, approved watch flight, heat outside the boundary, accessory fault, the FC's low-battery RTL, the FC's link-loss failsafe and the report.
- [x] Hardware and commercial evaluation (`docs/flame-hardware-evaluation.md`):
  - a monitoring kit fits the X650 Thermal on paper (about 4.15 kg, 66% of the 6.3 kg limit at 70% throttle);
  - an IGNIS III Mini-class ignition payload does not (about 5.77 kg, 92%, below 10 min usable, proprietary interface) and is out of scope.

### Phase 5.1 onwards: physical prototype (owner approval needed for each purchase)
- [ ] FLAME burn-monitoring kit (after the Thermal airframe exists):
  - build and weigh it;
  - CM4 report of attachment, latch, identity and temperatures;
  - CG and AutoTune with the kit, and measured endurance;
  - ZT6 radiometric interface and thermal accuracy against a controlled heat source;
  - heat standoff and height limits from tests near a small controlled fire;
  - ash and smoke maintenance. See `docs/flame-hardware-evaluation.md` §6.
- [ ] **Sentinel Lite bench, stage 1 (smallest purchase, about $830–1,030 at 2026-10-14 prices):** X500 V2 PX4 dev kit (propellers off) and an A8 mini, with a pack and charger; run `sentinel.drone.bench` against the acceptance criteria in `docs/drone-bench-testing.md`. Prove on a real Pixhawk 6C: the `MAV_OPTIONS` lockout, passive audit, `MAV2_*` stream rates, the gimbal angle via `MNT1_TYPE 8`, and A8 mini RTSP into the Hub with measured latency. Stage 2: HM30 (shared radio, range around a house). Stage 3: assemble and fly pilot-flown with a qualified pilot.
- [ ] Bench rig for Pro/Thermal (Pixhawk 6X + CM4 baseboard, M10, A8 mini; about $1.0–1.4k): bench steps 8.1–8.4 and 8.10 of the hardware doc; companion pairing on a real Pi; record results here.
- [ ] Video on real hardware: latency (target under 400 ms), the gimbal attitude source, `drone.video.camera` from the datasheet.
- [ ] Full V1 airframe, props off: assembly, weighing, every FC failsafe on the bench, companion power-loss, EMI, lidar check.
- [x] SITL: fence-breach and GPS/EKF-failure injection (Phase 5.0c fault scenarios).
- [ ] SITL: SF45/B proximity and BendyRuler configuration tested before hardware.
- [ ] MAVLink 2 signing between the companion and the FC (key derived from the pairing secret); mavlink-router with the same relay policy for the product.
- [ ] Regulatory and business track: aviation-law review, Standard 922 test plan, insurance (manufacturer and operator), operator model (customer pilots or a Sentinel service), privacy notice for drone video.
- [ ] Tethered, then open-field pilot-flown trials with Sentinel observing and proposing. Measure endurance, wind behaviour, link range around a house, thermal and visible image quality, and ground-position accuracy against surveyed markers.
- [ ] Thermal detection: licence-cleared thermal/aerial detector and labeled aerial footage before any thermal detection is claimed.
- [ ] Dock adapter (status and charging); manual charging is fine for V1.
- [ ] Later: "bring your own DJI Mini" (sub-250 g) as a software-only integration; NDAA parts list for any US version; Sentinel-commanded flight on a real aircraft only as a separate, gated decision.

## Phase 6: Commercial readiness
- [ ] Multi-site backend option (Postgres, object storage) behind the same store interface.
- [x] Installer and onboarding wizard (Phase 0.11: four installation paths, planner, device registry, test alerts). Still to do: run it with a non-technical household and a professional installer on real hardware.
- [ ] Privacy review (recording notices, retention defaults, data export and deletion), security review, and a pentest.
- [ ] Licence audit of every shipped model and dependency (`sources.md`).
- [ ] Support tooling: remote diagnostics bundle without media.

## Wilderness Guardian track (on `main`, off by default)

Consolidated into `main` on 2026-10-16 at the owner's request. Every Guardian feature is off by default (`guardian.enabled`, `guardian.species_classifier.enabled`), so V1 behaviour is unchanged unless a site turns it on.

Wildlife and human protection on the same Hub. Full plan, dataset review and limits: [docs/wilderness-guardian.md](docs/wilderness-guardian.md). Guardian phases are numbered separately from the phases above.

| Guardian phase | Status (2026-10-15) |
| --- | --- |
| G1 Wildlife recognition | Generic COCO animal labels through the existing detectors (implemented). **Species classifier implemented (2026-10-16), optional and off by default:** Google SpeciesNet v4.0.3a (Apache-2.0) on Sentinel's YOLOX boxes, with confident-only naming and "species uncertain" otherwise. On 435 real camera-trap images (ENA24, Caltech, NACTI), 160/161 confident calls on Sentinel boxes named the dataset's species. YOLOX found an animal in only 43 % of animal images. **Optional animal detector added (2026-10-16):** MegaDetector V6 Apache (`MDV6-apa-rtdetr-c`, threshold 0.6) next to YOLOX, adding only animal boxes YOLOX missed. Stills: 368/395 animal images found (YOLOX 168), species confirmed right end to end 321/395 (YOLOX 157), empty frames with a box 9/40 (YOLOX 0). 13 trail-camera clips: 91 % of animal-present frames (YOLOX 64 %; night 100 % vs 63 %), expected species confirmed on 8/11 clips (7), wrong confirmations 3 (1; elk called red deer). False boxes on moving water and rocks are set aside by Guardian when SpeciesNet agrees they are blank. About 2× detection time. MegaDetector likely trained on the LILA stills. No grizzly images yet; not run on owner footage ([docs/guardian-datasets.md](docs/guardian-datasets.md#animal-detector-megadetector-v6)). Dataset licences: CCT, NACTI, ENA24, WCS, WSU Lynx are CDLA-Permissive; Idaho forbids selling images; Animal Kingdom publishes no licence; MegaDetector V6 has AGPL, MIT and Apache variants |
| G2 Movement intelligence | **Implemented, synthetic sequences only**: guardian tracks with unambiguous re-acquisition, approach/retreat/lateral/stationary, relative speed, approach to camera/protected zone/person, turns, sustained approach, calibrated-only distance and time-to-zone, explainable levels, cooldowns, clip evidence, live overlay, `/api/v1/guardian`. Off by default. **Not run on real wildlife footage** |
| G3 Human protection | **Planned**. Owner facial recognition **blocked** (boundary 2); a mistaken identification must never trigger a physical response |
| G4 Emergency Guardian drone/rover | **Research concept**: no spray, smoke, payload-release, weapon or interception code; simulation at most |
| G5 Multi-platform Sentinel | **Planned**. The G2 monitor is platform-neutral (`moving_camera` caps levels); a ground rover is a candidate first physical Guardian prototype; Thermal/FLAME reused for agriculture |

### Decisions (2026-10-15)
1. Wildlife emergency alerts will eventually be able to push while home security is disarmed, through a separate configurable Guardian setting. Today they follow the arm mode and are not pushed when disarmed.
2. Approaching stays a recorded event without a push; Elevated Concern and Urgent Review notify (`guardian.notify_min_level`).
3. Owner facial recognition stays blocked until privacy and safety requirements are settled and recorded in `CLAUDE.md`.
4. Next priorities: validate G2 on a real camera (prerecorded wildlife clips, then live footage with labelled tracks via `sentinel.bench`), then species identification (licence-cleared classifier, e.g. an MIT/Apache MegaDetector v6 variant plus a species model, benchmarked before adoption).

### Next steps
- [ ] Run G2 on prerecorded wildlife clips and on the owner's camera (steps: `docs/wilderness-guardian.md#real-camera-validation-plan`); record false-alarm counts per day and level.
- [ ] Label animal tracks in real footage (approach / retreat / lateral) and measure movement classification against them.
- [ ] Check a ground calibration against tape-measured distances.
- [x] Verify dataset and weight licences (`sources.md`) before any download; evaluate MegaDetector v6 and a species classifier (done 2026-10-16: `scripts/guardian/animal_detector_eval.py`, `scripts/guardian/video_eval.py`).
- [ ] Run `scripts/guardian/video_eval.py` on the owner's day and night footage with the animal detector and species classifier on; record false Guardian tracks per day.
- [ ] Elk confirmed as red deer (3 short tracks in the video test; red deer is inside SpeciesNet's Canadian geofence): decide whether Guardian should treat elk / red deer as one group in North America.
- [ ] Measure the add-on on the GTX 1070 machine; on the Iris Xe laptop it costs 460–750 ms per frame.
- [ ] Separate disarmed-mode wildlife alert setting (decision 1), with tests.

## Guardian animal detector findings (2026-10-16)

Machine: i5-1135G7 + Iris Xe (DirectML). Data: 435 camera-trap stills (ENA24, Caltech, NACTI; MegaDetector was probably trained on them) and 13 Wikimedia Commons trail-camera clips (not LILA). Reports: `bench_runs/guardian_detectors/v1*`, `bench_runs/guardian_video/final*` (git-ignored). Detail: [docs/guardian-datasets.md](docs/guardian-datasets.md#animal-detector-megadetector-v6).

| Configuration | Stills: animal images found | Stills: empty frames with a box | Stills: species right end to end | Video: animal frames found (night) | Video: empty frames with a shown track | Video: expected species / wrong | Detect p50 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| YOLOX-s 960 (0.5), current default | 168/395 | 0/40 | 157/395 | 64 % (63 %) | 0/360 | 7/11 / 1 | 147 ms |
| MegaDetector V6 Apache alone (0.6) | 367/395 | 9/40 | 324/395 | 87 % (100 %) | 21/360 | 8/11 / 1 | 220 ms |
| **YOLOX + MegaDetector add-on (0.6)** | 368/395 | 9/40 | 321/395 | 91 % (100 %) | 21/360 | 8/11 / 3 | about 2× YOLOX |

Selected: the add-on, for wildlife cameras only (people and vehicles stay on the benchmarked YOLOX; nothing changes unless `detector.animal_detector` is set). MegaDetector's threshold is 0.6, not 0.4: same species results on stills with 9 instead of 32 empty-frame boxes. Generic-only tracks that SpeciesNet agrees are `blank` are set aside by Guardian (river clip: 4 false tracks, all set aside, about 1 s on screen each).

## Open risks
- Real-world detector accuracy at night and in bad weather is unknown until Phase 1.
- Alert fatigue is the main product risk. Every rule needs a measured nuisance rate.
- Wilderness Guardian movement levels are verified on synthetic sequences only; real animal movement, night IR and the missing species classes (COCO has no cougar, wolf, coyote, moose or elk) may make them unreliable until G1/G2 are validated on real footage.
- Drone operations face strict regulation. A commercial unattended drone may never be legal in some markets, so the product must stand on cameras alone. In Canada, a Sentinel-branded drone for residential use needs a Standard 922 declaration and customers with Advanced certificates. A US version would need a separate NDAA-style parts list because of the FCC Covered List.
- Drone product risk: the value is the integration (incident-driven, audited, evidence-linked inspections), but suburban use needs a Standard 922 declaration and Advanced-certificate pilots, and thermal detection is not yet validated. Validate demand with rural and small-business pilot customers before committing to production.
