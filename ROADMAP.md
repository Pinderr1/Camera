# Sentinel roadmap

Last updated: 2026-10-13. Priority: **a working security brain on real cameras before any drone hardware.** The real-footage benchmark (below) shows that **person detection is the bottleneck**. Fix that before adding features.

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

## Real-footage benchmark findings (2026-10-13)

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
- [ ] **Detector upgrade (top priority, from benchmark findings):** add an ONNX Runtime detector (YOLOX or RT-DETR with Apache-2.0 weights, 640 px input, optional OpenVINO/DirectML/CUDA). Target at least 70% recall on MOT16-09 at 90% or better precision, and at least 50% on MOT16-02, within a 5 fps per camera budget. Verify with `sentinel.bench`.
- [ ] Record and label 1-2 hours from the target residential cameras (day, night/IR, rain, deliveries, ordinary family activity). Box labels via CVAT (MOT export), plus hand-written expected events and `expect_no_notifications` for ordinary footage. This replaces public footage as the acceptance set.
- [ ] Connect 2-3 real RTSP cameras on sub-streams (640p, 5-10 fps; measured 21% CPU for 3 cameras on this host). Run `sentinel.bench live` for 24 h and record latency, dropped frames, and reconnects.
- [ ] Tracker upgrade (ByteTrack or OC-SORT style, using low-score detections) once recall improves. Measure identity switches and "mostly tracked" before and after.
- [ ] Group-box handling: the detector often emits one box for two people walking together. Evaluate whether the stronger detector removes this before adding logic.
- [ ] Multi-view dataset for cross-camera correlation validation (own cameras with overlapping or adjacent views).
- [ ] Zone editor in the dashboard: draw polygons on a snapshot and save to a local config, so nobody hand-edits JSON.
- [ ] Calibrate topology transit times from observed walks. Add a tool that suggests `min/max_transit_s` from stored site tracks.
- [x] Detector benchmark harness and first comparison (Lite0, Lite2, and tiles on public footage; see findings). Still needed: real IR/night and rain recordings, and vehicle-class labels.
- [ ] Label review loop: export resolved incidents (`expected_activity`, `false_alarm`, `real_concern`) and report nuisance rate per rule.
- [ ] Clip playback: H.264 encoding via a bundled ffmpeg or PyAV fallback when OpenCV lacks `avc1`.
- [ ] Night-specific handling: IR mode switch, headlight glare suppression, spider-web and insect false boxes (minimum track displacement or persistence).
- [ ] Event deduplication when two overlapping cameras see the same person (`overlap: true` links tested on real views).

## Phase 2: Pilot hardening
Exit: a non-technical household runs it for 30 days without help.
- [ ] Service install (Windows service / systemd), auto-start, watchdog, log rotation, disk-space guard.
- [ ] HTTPS via a reverse proxy recipe (Caddy) and a VPN recipe (Tailscale/WireGuard).
- [ ] User accounts with roles (owner, member, viewer), per-user tokens, session expiry. Replace the single shared token.
- [ ] Secrets: OS keyring or `.env` loaded by the service manager. Document rotation.
- [ ] Backup and restore of config, settings, and the SQLite store.
- [ ] Health page: per-camera uptime, detector latency, queue depth, storage use. A heartbeat to an external watchdog (healthchecks.io pattern from BedWatch).
- [ ] Schedules: automatic arm and disarm by time, and geofenced presence from phones (Phase 3 app).
- [ ] Self-hosted ntfy or push relay. Notification action buttons (acknowledge, expected activity).

## Phase 3: Mobile app
Exit: an iOS/Android app with push, live view, timeline, and arm/disarm.
- [ ] React Native/Expo app against `/api/v1`. Device registration endpoint (`/api/v1/devices`) and APNs/FCM relay.
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
Exit for each step is listed in the drone doc. Never skip steps.
- [ ] ArduPilot SITL telemetry through `MavlinkTelemetryAdapter`.
- [ ] Flight commands implemented against SITL only, behind an explicit config flag and a SITL check.
- [ ] Failsafe injection tests in SITL.
- [ ] Drone video stream ingested as a mobile camera source.
- [ ] Dock adapter (status and charging).
- [ ] Regulatory review, then bench, tethered, and supervised field trials with a certified pilot.

## Phase 6: Commercial readiness
- [ ] Multi-site backend option (Postgres, object storage) behind the same store interface.
- [ ] Installer and onboarding wizard (camera discovery via ONVIF, zone drawing, test alerts).
- [ ] Privacy review (recording notices, retention defaults, data export and deletion), security review, and a pentest.
- [ ] Licence audit of every shipped model and dependency (`sources.md`).
- [ ] Support tooling: remote diagnostics bundle without media.

## Open risks
- Real-world detector accuracy at night and in bad weather is unknown until Phase 1.
- Alert fatigue is the main product risk. Every rule needs a measured nuisance rate.
- Drone operations face strict regulation. A commercial unattended drone may never be legal in some markets, so the product must stand on cameras alone.
