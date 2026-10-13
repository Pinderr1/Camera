# Sentinel roadmap

Last updated: 2026-10-12. Priority: **a working security brain on real cameras before any drone hardware.**

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

## Phase 1: Brain on real cameras (next)
Exit: 2+ real IP cameras running for 7 days with useful incidents and fewer than 1 nuisance push per day.
- [ ] Connect 2–3 real RTSP cameras (sub-stream at 640p, 5 fps). Measure CPU load per camera on the target box.
- [ ] Zone editor in the dashboard: draw polygons on a snapshot and save to a local config, so nobody hand-edits JSON.
- [ ] Calibrate topology transit times from observed walks. Add a tool that suggests `min/max_transit_s` from stored site tracks.
- [ ] Detector benchmark on real day, night (IR), and rain footage: lite0 vs lite2 vs an ONNX model. Record recall per class and false boxes per hour.
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
