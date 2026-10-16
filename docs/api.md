# Sentinel API (v1)

The dashboard and any future mobile app share this one API. Interactive OpenAPI docs are served at `/api/docs`, and the schema at `/api/openapi.json`.

## Authentication

- Set `SENTINEL_API_TOKEN` in the service environment. Without it, the server only binds to loopback and auth is disabled. Use that for local development only.
- **Apps:** send `Authorization: Bearer <token>`.
- **Browser:** `POST /api/v1/session {"token": "..."}` sets an HttpOnly, SameSite=Strict cookie (marked Secure over HTTPS), so `<img>` MJPEG and `EventSource` work without the token appearing in URLs. `DELETE /api/v1/session` signs out.
- Optional header `X-Sentinel-Operator: <name>` sets the actor recorded in the audit log.
- Remote access: put the server behind a VPN (WireGuard/Tailscale) or an authenticated HTTPS reverse proxy. Never port-forward it.

## Resources

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/health` | No auth. Liveness only |
| GET | `/api/v1/site` | Site map, cameras (no source URLs), zones, topology, rules, privacy, `drone_enabled`, `drone_points` (drone inspection locations) |
| GET | `/api/v1/state` | Settings, camera status, open incidents, active site tracks, `coverage` (`state`: `full\|unverified\|degraded\|none\|not_running\|no_cameras`, `monitoring`, `total`, `not_monitoring[]`, `components`) |
| GET | `/api/v1/cameras` | Cameras with `status`: `health` (`state`, `label`, `monitoring`, `since_ms`, `reason`, `warnings`), `connected`, `fps`, `target_fps`, `last_frame_ms`, `error`, `metrics`, `detector_state`, `worker_state`, `restarts_total` |
| GET | `/api/v1/cameras/{id}/snapshot.jpg` | Latest annotated frame (privacy-masked). `X-Sentinel-Monitoring: true\|false` and `X-Frame-Age-Ms` say whether it is live |
| POST | `/api/v1/cameras/{id}/restart` | Restart the camera's processing and reset its restart budget. 409 when cameras are not running in this session |
| GET | `/api/v1/cameras/{id}/stream.mjpg` | MJPEG preview at the camera's fps |
| GET | `/api/v1/events` | Filters: `since_ms`, `until_ms`, `camera_id`, `min_severity`, `incident_id`, `limit` (≤1000) |
| GET | `/api/v1/incidents` | Filters: `status=open\|closed`, `since_ms`, `limit` |
| GET | `/api/v1/incidents/{id}` | Incident plus its events |
| POST | `/api/v1/incidents/{id}/acknowledge` | Idempotent |
| POST | `/api/v1/incidents/{id}/resolve` | `{"outcome": "expected_activity\|false_alarm\|real_concern\|unclear"}` |
| GET | `/api/v1/search/incidents` | Filters: `since_ms`, `until_ms`, `camera_id`, `zone_id`, `label`, `min_severity`, `outcome` (`expected_activity\|false_alarm\|real_concern\|unclear\|unreviewed`), `rule_id`, `q` (text), `limit` |
| GET | `/api/v1/incidents/{id}/explanation` | Structured explanation: `headline`, `objects`, `cameras` (with `basis`), `zones`, `sequence` (each `observed` or `inferred`), `rules` (plain description, matched details, downgrades), `confidence` (detection range, cross-camera links, uncertainty), `severity.reasons`, `evidence` (drone snapshots as `drone_snapshot`), `statement`, and `drone_inspections` when the drone inspected for this incident (reported separately from the fixed cameras) |
| POST | `/api/v1/incidents/{id}/notes` | `{"text"}`, appended (never edited) and audited |
| GET | `/api/v1/incidents/{id}/export` | Zip: `report.html`, `report.json`, `manifest.json` (SHA-256 per file), and with `?include_media=true` the recordings. Evidence is reported as intact, changed, missing, or not fingerprinted. Audited |
| POST / GET | `/api/v1/missed` | Report activity Sentinel did not report: `{"occurred_ms", "camera_id", "zone_id"?, "category"?, "description"?}`. Stores what Sentinel recorded on that camera within ±5 min. `GET ?days=` lists reports |
| GET | `/api/v1/reports/performance?days=` | Per rule and per camera: matches, recorded-only matches, incidents, notifications, outcomes, `nuisance_rate`, `genuine_rate`, missed reports |
| GET | `/api/v1/insights?days=` | Activity statistics, changes, unusual observations (information only) and suggestions; see [intelligence.md](./intelligence.md) |
| POST | `/api/v1/incidents/{id}/protect` | `{"protected": true\|false}`. Protected recordings skip disk-space purges and use `retention_days.protected` |
| GET | `/api/v1/media/{path}` | Clips and snapshots referenced by events and incidents |
| GET / PATCH | `/api/v1/settings` | `arm_mode` (`disarmed\|home\|away`), `sensitivity` (`low\|normal\|high`), `notifications_enabled` |
| GET | `/api/v1/audit` | Operator and system actions |
| GET | `/api/v1/stream` | Server-sent events: `event`, `incident`, `notification`, `settings`, `drone`, `drone_inspection` (an inspection finished), `config`, `health` (the coverage object, on every health transition) |
| GET | `/api/v1/drone` | Primary drone: `drone_id`, `adapter`, `physical`, `mode` (`propose`: the pilot flies exported routes; `command`: simulators only) and `mode_reason`, `telemetry` (including flight mode, landed state, EKF, pre-arm, HDOP, wind, gimbal angle, recent FC messages), `flight_state`, `safety_state`, `preflight_blockers`, `readiness`, `parameter_audit`, `conditions`, `advisories`, `companion`, `video`, `flight` (open flight), `maintenance`, `requests` (each with `aircraft` and, once approved, `flight_estimate`), `geofence`, `safe_transit_altitude_m`, `home`, `model` (`id`, `name`, `tier`, `status`, `camera`, `capabilities`, `options`, `limitations`, `label`), `health` (`state` ok/info/warn/critical, `conditions` [{`id`, `severity`, `text`, `evidence`}] for stale, implausible or alarming telemetry, `return_home_possible` and why, battery `drain` against the model estimate), `records_ok` (false after a record write failed), `journey` (see `/drone/journey`). Telemetry adds per-signal times (`gps_ms`, `battery_ms`, `gimbal_ms`, `ekf_ms`), rejected and jumping positions, `fc_reboots`, `link_signed`, `link_delay_ms`, `battery_state`, `fence_breach`, `other_vehicles`. `video` adds `stream_check` and the states `unverified`, `ready`, `unreachable`, `frozen`, `blank`, `no_video` |
| GET | `/api/v1/drone/readiness` | `{"ready", "mode", "model", "blockers", "checks": [{"id", "label", "state": "pass\|fail\|warn\|na", "detail", "blocker"}]}`. Model checks include `lockout`, `daylight`, a required `companion` or `video`; approval also refuses routes that exceed the model's estimated endurance (`route_exceeds_endurance`) |
| GET | `/api/v1/drone/link` | MAVLink link state, reconnects, parameter count, fence vertices (never credentials) |
| POST | `/api/v1/drone/conditions` | Operator's on-site confirmation: `{"precipitation": bool, "visibility_ok": bool, "wind_mps"?, "temperature_c"?, "note"?, "daylight"?: bool, "lights_ok"?: bool}` (valid `drone.weather.conditions_ttl_s`). Day-only models refuse approval when `daylight` is false; night flights need `lights_ok` |
| GET | `/api/v1/drone/models` | Drone model comparison: `models` (Lite, Pro, Thermal with hardware, capabilities, camera, flight estimates, requirements, options, limitations, `support_note`), `rows` (capability by model: `yes`/`optional`/`limited`/`no`), `facts`, `shared`, `all` (including `generic_ardupilot`), `capabilities`. No prices. Available before any drone is registered |
| POST | `/api/v1/drone/models/recommend` | `{"night"?, "windy"?, "zoom"?, "obstacles"?, "property_size_m"?, "budget"?}` → `{"model", "name", "reasons", "limitations", "cautions", "alternatives"}` |
| GET | `/api/v1/drone/diagnosis` | `?drone_id=` (default primary) → `{"summary", "findings": [{"check", "label", "state", "detail", "blocks_approval", "causes", "steps"}], "next_step": {"check", "label", "step", "cause", "remaining_steps"} | null, "retest"}`, model-specific; findings include live flight-health conditions (`health:<id>`) and the drone camera (`video:<state>`) when no readiness check already covers them. 404 for planned or unknown drones |
| GET | `/api/v1/drone/journey` | The drone setup journey from evidence only: `{"level", "level_text", "level_index", "steps": [{"id", "title", "state": "done\|attention\|todo", "detail", "action"}], "next", "simulated", "records", "notes"}`. Levels: `planned`, `simulated`, `hardware_detected`, `connected`, `partially_configured`, `configured`, `bench_verified` (a passing bench record matching the current parameter fingerprint), `flight_test_verified`, `operationally_authorized` (records a named person made). None of them enables flight commands to a real aircraft |
| POST | `/api/v1/drone/inspections` | `{"zone_id": "..."}`, `{"point_id": "..."}` (one of `drone.inspection_points`), or `{"lat":..,"lon":..}`, plus optional `incident_id`. Creates a pending request on the primary drone; this never flies |
| GET | `/api/v1/drone/inspections` | Stored inspections (`?incident_id=`, `limit`), including finished ones with `observations` and `evidence_paths` |
| GET | `/api/v1/drone/inspections/{id}` | One inspection: status, phase, mode, route, times, flight id, observations |
| GET | `/api/v1/drone/inspections/{id}/route` | The approved route, or a `preview` of the route approval would produce |
| GET | `/api/v1/drone/inspections/{id}/mission` | Download for a ground station: `?format=plan` (QGroundControl JSON, with the geofence) or `waypoints` (`QGC WPL 110`). Audited |
| POST | `/api/v1/drone/inspections/{id}/approve` | Operator approval. Fails with 409 while readiness has blockers or the route leaves the geofence. `propose` mode: status `approved` until the pilot flies it within `approval_window_s`. `command` mode (simulators): Sentinel flies it |
| POST | `/api/v1/drone/inspections/{id}/deny` | `{"note": "..."}` |
| POST | `/api/v1/drone/commands` | `{"action": "takeoff\|goto\|hold\|return_home\|land", "params": {...}}`. Simulators only; real aircraft refuse (409) |
| GET | `/api/v1/drone/flights` | Flight log: start/end, duration, max altitude, distance, battery, flight states, FC messages, safety events, linked inspection |
| GET, POST | `/api/v1/drone/maintenance` | Records (`inspection`, `repair`, `battery`, `propellers`, `firmware`, `calibration`, `commissioning`, `equipment_check`, `bench_test`, `flight_test`, `authorization`, `note`) and a summary. POST `{"kind", "note"?, "verdict"?: "pass\|fail"}`: `bench_test` and `flight_test` need a verdict; those and `authorization` are refused for the built-in simulator and store the current parameter fingerprint. `commissioning` stores the FC parameters as the reference for later change detection (not returned) |
| GET | `/api/v1/drone/video.mjpg` | Annotated drone camera during an inspection (404 without `drone.video`) |
| GET | `/api/v1/drone/companions` | Paired companion computers with state (`reporting`/`silent`/`revoked`) and their last health report. Never secrets |
| POST | `/api/v1/drone/companions/code` | `{"drone_id"?}` → `{"code": "ABCD-EFGH", "expires_ms"}`: one-time pairing code, 10 minutes |
| DELETE | `/api/v1/drone/companions/{id}` | Revoke: sessions end, the secret is deleted |
| POST | `/api/v1/drone/companion/pair` | **Device endpoint** (no operator token): `{"code", "name", "hardware"}` → `{"companion_id", "drone_id", "secret"}` once. 403 for wrong or expired codes; pairing pauses after 10 failures |
| POST | `/api/v1/drone/companion/session` | **Device endpoint**: `{"companion_id", "timestamp_ms", "nonce", "mac"}` with `mac = HMAC-SHA256(secret, "sentinel-companion-session-v1\|id\|timestamp\|nonce")` → `{"token", "expires_ms"}` (30 min). Replays and clocks off by more than 120 s are refused |
| POST | `/api/v1/drone/companion/report` | **Device endpoint**, `Authorization: Companion <token>`: `{"health", "relay", "video"}` |

### Security rules

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/rules` | `rules` (each with `display_name` and `plain` {what, when, then}), `templates`, `catalog` (types, classes, modes, days, severities, zones, zone types, scenarios, mode notification levels) |
| POST | `/api/v1/rules` | Create from builder fields; the id comes from the name. Validated with the whole config (422 on errors), saved, audited, and applied live |
| PUT / DELETE | `/api/v1/rules/{id}` | Replace or delete. A rule used by a combination cannot be deleted (422) |
| PUT | `/api/v1/rules/{id}/enabled` | `{"enabled": bool}` |
| POST | `/api/v1/rules/check` | Validate and describe a draft without saving: `{"valid", "errors", "plain"}` |
| POST | `/api/v1/rules/test` | `{"rule", "source": "walk\|scenario\|history", "arm_mode", "start"?, "steps"?: [{"zone_id","stay_s","gap_s"}], "label"?, "scenario"?, "days"?}`. Runs the real engine on a throwaway database; returns `fired`, `incidents`, `notifications`, `other_rules_fired`, `notes`, `verdict`. Audited |

`PATCH /api/v1/settings` accepts `arm_mode` `disarmed|home|away|night|vacation`.

### System health and backups

See [reliability.md](./reliability.md). None of these return stream addresses or passwords; backup contents never leave the server.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/system/health` | `coverage`; `cameras[]` (health, fps, `metrics` with `detect_ms_p50/p95`, `latency_ms_p50/p95`, `frames_processed`, `frames_skipped`; reconnects, restarts, detector provider and state); `resources` (CPU, memory, NVIDIA GPU, disk); `storage` (free space, threshold, recording paused, media size); `database` (write failures, last recovery); `backups`; `service`; recent `notifications` |
| GET | `/api/v1/system/outages` | Filters: `since_ms`, `camera_id`, `limit`. Each: `outage_id`, `scope` (`camera\|storage\|database\|service\|process\|notifications`), `kind`, `states`, `camera_id`, `started_ms`, `ended_ms` (null while ongoing), `duration_s`, `reason`, `notified` |
| GET | `/api/v1/system/backups` | Name, `created_ms`, `label`, `files`, `size_bytes`, `includes_credentials` |
| POST | `/api/v1/system/backups` | `{"label": "manual"}`. Writes a backup now |
| POST | `/api/v1/system/backups/{name}/verify` | `{"ok", "problems"}` |
| POST | `/api/v1/system/backups/{name}/restore` | `{"parts": ["config", "settings", "database"]}`. Writes a `pre-restore` backup, applies config and settings now, and stages the database for the next start. Returns `applied`, `staged`, `safety_backup` |

### Setup (configuration)

All changes are validated as a whole, saved to `<storage>/site.json` with backups, audited, published on the `config` SSE topic, and applied to the running pipeline. Only cameras whose settings changed are restarted.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/config` | Editable site config. Camera `source` is the safe public view (no URLs, paths, or passwords). `catalog` lists zone types, categories (with defaults and COCO labels), source types, and brand templates |
| POST | `/api/v1/config/cameras` | `{"name", "fps", "enabled", "draft_id"}`, where `draft_id` comes from a connection test. Returns `{"id"}` |
| PUT | `/api/v1/config/cameras/{id}` | Same fields. A new `draft_id` replaces the source; a `source` object can be sent instead (write-only fields are kept when omitted) |
| DELETE | `/api/v1/config/cameras/{id}` | Removes zones from rules (disabling rules left without zones), drops links, deletes the stored credential |
| PUT | `/api/v1/config/camera-order` | `{"order": [ids]}` |
| PUT | `/api/v1/config/cameras/{id}/zones` | `{"zones": [{"id"?, "name", "type", "polygon"}], "privacy_masks": [polygon]}`, normalized 0–1. Returns `{"zones", "notes"}` |
| PUT | `/api/v1/config/map` | `{"width_m", "height_m", "cameras": [{"id","x","y","heading_deg","fov_deg"}], "zones": [{"id","map_polygon"\|null}]}`, in metres (x east, y south) |
| PUT / GET | `/api/v1/config/map/image` | Raw PNG/JPEG body (≤ 10 MB) / the stored image |
| PUT | `/api/v1/config/topology` | `{"links": [{"from","to","min_transit_s","max_transit_s","overlap","bidirectional","from_zones","to_zones"}]}` |
| PUT | `/api/v1/config/categories` | `{"categories": {"animal": {"detect": true, "alerts": false}, ...}}` site-wide |
| PUT | `/api/v1/config/cameras/{id}/categories` | Per-camera override |
| GET | `/api/v1/cameras/{id}/frame.jpg` | Clean (privacy-masked, unannotated) frame for the editors, refreshed once a second |

### Onboarding

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/onboarding/test` | `{"source": {..., "password"?}, "camera_id"?}`, `{"draft_id"}`, or `{"camera_id"}` (stored source). Returns `ok`, `kind`, `message`, per-stream `resolution`/`measured_fps`/`codec`/`hw_decode_active`, `ai_stream`, `diagnostics`, `snapshot_jpeg_b64`, and `draft_id`. Never echoes addresses or passwords |
| GET | `/api/v1/onboarding/templates` | Brand templates (labels and whether they have sub-streams and snapshots) |
| GET | `/api/v1/onboarding/decode` | Hardware-decoding capability of this build |
| POST | `/api/v1/onboarding/local-devices` | Probes USB/capture device indices (opens devices briefly) |
| POST | `/api/v1/onboarding/onvif/discover` | WS-Discovery (3 s) on the local segment |
| POST | `/api/v1/onboarding/onvif/inspect` | `{"xaddr", "username", "password"}`. Returns device info, profile summaries, and a `draft_id` |
| GET | `/api/v1/onboarding/network?refresh=` | This computer's adapters (name, type, IPv4/prefix, gateway, DHCP, DNS, Windows network profile), firewall profiles, network-level `findings`, and the troubleshooter's `brands`. Cached 30 s; `refresh=true` re-reads. No MAC addresses, Wi-Fi names, or public IPs |
| POST | `/api/v1/onboarding/network/diagnose` | `{"host", "brand"?, "device_mask"?, "device_gateway"?, "rtsp_port"?, "http_port"?, "discover"?}`. Probes one private address (ping, TCP 80/443/554/8000/9000/37777, the routers involved, optional SADP/ONVIF) and returns `reachable`, `summary`, `route`, `checks`, `identity`, `discovered`, and `findings`. Public addresses and bad masks: 422 |
| POST | `/api/v1/onboarding/network/discover` | `{"confirm": true, "sadp"?, "onvif"?, "port_check"?}`. User-approved search of this computer's own networks. `port_check` covers at most the /24 around each local address. Without `confirm`: 422. Returns `devices` (with `on_local_network` and `network`), `networks_searched`, `notes` |
| POST | `/api/v1/onboarding/channels` | `{"source": {..., "password"?}}` or `{"draft_id"}`, `max_channels` (≤ 64). RTSP DESCRIBE per recorder channel (stops on the first login failure). Each channel that answers carries its own `draft_id` for `POST /config/cameras` |

| GET | `/api/v1/onboarding/guidance/new-cameras` | Symptom → checks for new PoE, Wi-Fi and login/codec problems (read-only guidance) |

A **finding** is `{"code", "status", "title", "explanation", "evidence": [...], "steps": [...], "data": {...}}`. `status` is `confirmed` (Sentinel observed the fault), `suspected` (a likely cause the evidence cannot prove), `ruled_out` (checked, not the problem), `info`, or `ok`. Codes are stable identifiers (for example `unreachable`, `subnet_mismatch`, `rtsp_closed`, `vpn_route`, `public_profile`, `found_elsewhere` with `data.host`); titles and text may change. Diagnosis, discovery, and channel searches are written to the audit log (address and outcome only).

### Devices and installation

See [installation.md](./installation.md). Device registration records what is installed; `level` (`registered\|connected\|video\|ai_verified`) is earned only from data Sentinel received.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/devices` | `devices[]`: this computer (`hub_local`), cameras, recorders, drones, docks, sensors. Each has `id`, `category`, `name`, `profile`, `support`, `simulated`, `capabilities`, `level`, `state`, `detail`, `warnings`, `removable`, plus category fields (camera `zones`/`privacy_masks`/`recorder`; recorder `host`/`channels_added`; drone `battery_pct`/`docked`/`primary`/`safety_state`; sensor `battery_pct`/`last_event`). Never stream addresses or passwords |
| GET | `/api/v1/devices/catalog` | `categories`, `registrable`, `capabilities`, `support_levels`, `levels`, `profiles` (hardware compatibility profiles) |
| POST | `/api/v1/devices` | Register a recorder, drone, dock or sensor: `{"category", "name", "profile"?, "simulated"?, "map"?: {"x","y"}, "zone_id"?, "host"?, "channels"?, "model"?, "connection"?, "indoor"?, "drone_id"?, "drone_model"?, "drone_options"?, "video_url"? (write-only; responses show `url_set`)}`. Cameras use `/config/cameras`. Unsupported profiles, unknown drone models and options a model does not offer: 422 |
| PUT | `/api/v1/devices/{id}` | Same fields (category cannot change) |
| DELETE | `/api/v1/devices/{id}` | Removes a device **or a camera** and unlinks it (recorder links, docks, primary drone, plan items). Returns `notes`, `plan_items_unlinked` |
| POST | `/api/v1/devices/{id}/test` | Simulated sensors only: records an `info` `sensor_test` event (never an incident) |
| PUT | `/api/v1/config/drone` | `{"enabled"?, "device"? (primary), "home_map"?, "geofence"?: {"property": true} or {"map_polygon", "min_altitude_m", "max_altitude_m"}, "inspection_points"?: [{"name", "map": [x, y]}]}`. Needs `site.map.origin_geo` |
| PUT | `/api/v1/config/notifications` | `{"ntfy"?: {"enabled", "server", "topic"? (stored in the credential store), "attach_snapshot"}, "mqtt"?: {"enabled"}}`. Applied without a restart. Returns the notification view (never the topic) |
| GET | `/api/v1/installation` | `state` (path, property, questionnaire, recommendation, plan, step marks, current step, completion), `steps`, `paths`, `property_types`, `site`, `devices`, `readiness` (`levels`, `statement`, `checks[]` with `step`/`status` `pass\|warn\|fail\|pending`/`blocking`, `steps`, `blocking`, `open_issues`, `can_complete`), `drone`, `notifications`, `processing` |
| PUT | `/api/v1/installation` | `{"path"?, "property"?, "current_step"?, "steps"?: {"plan": "done\|skipped\|pending"}}` |
| POST | `/api/v1/installation/property` | `{"name", "property_type", "width_m", "height_m", "latitude"?, "longitude"?, "notes"?}`. Saves the site name and map size (and location) through the config editor |
| POST | `/api/v1/installation/recommend` | Questionnaire → camera suggestion (saved with the installation) |
| GET / PUT | `/api/v1/installation/plan` | The property plan `{"items": [...]}`. PUT validates, saves, and returns the plan with `coverage` |
| POST | `/api/v1/installation/plan/coverage` | Coverage analysis and equipment `estimate` for a posted (unsaved) plan, or the saved one when `items` is absent |
| GET | `/api/v1/installation/report` / `report.html` | Installation plan as JSON, or as a printable self-contained page (`?download=true` for a file) |
| POST | `/api/v1/installation/test-notification` | Sends a labelled test through every enabled channel and returns per-channel outcomes |
| POST | `/api/v1/installation/complete` | `{"acknowledge_issues": bool}`. 409 while a blocking check fails, or when open issues are not acknowledged |
| POST | `/api/v1/installation/reopen` / `reset` | Reopen a completed installation; reset the journey (`{"keep_plan": true}`), keeping cameras, devices and settings |

### Assistant

Sentinel AI Guide; see [assistant.md](./assistant.md). Conversations live on the hub. Messages, cards and actions never contain passwords, API keys or stream addresses.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/assistant` | `mode` (`ai` or `guide`), `provider`, `provider_display`, `model`, `configured_provider`, `configured_model`, `providers` (choices with their models), `external`, `reason` (why the built-in guide is answering), `privacy` (what is shared), `starters`, `welcome` |
| PUT | `/api/v1/assistant/settings` | `{"provider"?: "none\|deepseek\|anthropic", "model"? (`deepseek-flash`, `deepseek-v4-pro`, `claude-opus-5-5`), "effort"?, "share_network_details"?, "api_key"? (write-only, credential store), "key_provider"? (`deepseek\|anthropic`; default: the selected provider), "forget_api_key"?}`. Validated through the config editor; returns the status |
| GET / POST | `/api/v1/assistant/conversations` | List (id, title, updated, message count) / create |
| GET / DELETE | `/api/v1/assistant/conversations/{id}` | `messages[]`: `{id, role: user\|assistant\|note, text, source: ai\|guide\|system, cards[], actions[], ms}` / delete permanently |
| POST | `/api/v1/assistant/conversations/{id}/messages` | `{"text"}` (≤ 2000 chars; passwords and keys are removed before storing). Returns the new messages. Cards: `choices`, `proposal` (`action_id`, `title`, `summary`, `effects`, `status`), `recorder_login` (host, brand, template, port), `preview` (camera ids), `devices_found`. Actions: `{"type": "open", "target", "label", ...}` |
| POST | `/api/v1/assistant/conversations/{id}/actions/{action_id}` | `{"confirm": bool}`. Confirm runs the existing backend call and returns its result; 409 if the proposal is no longer pending |
| POST | `/api/v1/assistant/conversations/{id}/events` | `{"event": "channels_scanned\|cameras_added"}` after the secure sign-in form; the outcome is read from Sentinel's own audit log and config, not from the browser |

Errors use standard status codes with `{"detail": "..."}`: 401 (auth), 404 (unknown id), 409 (drone rejected, for example preflight blocked; camera processing not running), 422 (validation), 507 (backup could not be written).

## Wilderness Guardian

`GET /api/v1/guardian[?camera_id=]` returns `enabled`, `levels`, `notify_min_level`, `calibrated_cameras` and `tracks[]`. Each track has `guardian_track_id`, `camera_id`, `track_id`, `kind`, `species` (display, concern, status, note, detector_label), `bbox`, `trail`, `lost`, `reacquired`, `ambiguous_with`, `movement` (movement, relative_speed in body heights/s, growth_rate, image_heading, turned, sped_up, reasons), `targets`, `estimates` (metres and seconds only when the camera is calibrated) and `assessment` (level, level_name, score, confidence, factors, held_back). `GET /api/v1/state` includes the same tracks under `guardian`. Wildlife events have `event_type` `wildlife_approaching`, `wildlife_elevated_concern` or `wildlife_urgent_review`, with the assessment in `details`. See [wilderness-guardian.md](wilderness-guardian.md).

## Optional accessories and FLAME Mode

All routes below require the same authentication as other data endpoints.
Accessory registration/removal uses the existing `/api/v1/config/devices` routes
with category `accessory` and its `drone_id`. See [FLAME Mode](flame.md) for scope,
simulation labels, persistence and hardware limitations.

| Method | Path under `/api/v1/flame` | Purpose |
| --- | --- | --- |
| GET | (root), `/compatibility` | Overview, accessory compatibility profiles and matrix |
| GET | `/accessories/{id}` | Received/simulated health, attachment, compatibility and records |
| POST | `/accessories/{id}/records`, `/accessories/{id}/simulate` | Record inspection/maintenance/commissioning; inject a simulated fault only |
| PUT | `/drones/{id}/configuration` | Ground-only switch after configuration checks |
| GET | `/drones/{id}/position` | Current usable position for recording a field corner |
| POST | `/qualifications`, `/authorizations` | Record entered qualifications/authorization information |
| DELETE | `/records/{id}` | Withdraw a record while retaining history |
| POST | `/fields` | Save a field and its boundary |
| GET, PUT, DELETE | `/fields/{id}` | Read, edit or remove a field (operation constraints apply) |
| GET, POST | `/operations` | List or create operations |
| GET, PUT | `/operations/{id}` | Operation, checklist, crew, equipment and recorded observations |
| POST | `/operations/{id}/weather`, `/observations` | Record weather or crew observations |
| POST | `/operations/{id}/start`, `/watch`, `/post-burn`, `/stand-down`, `/close` | Monitoring workflow; watch creates an inspection request requiring separate approval |
| POST | `/operations/{id}/ignition` | Record the crew's report; does not control ignition |
| POST | `/operations/{id}/alert`, `/simulate` | Operator message to configured people; simulated faults only |
| GET | `/operations/{id}/report?format=json\|html` | Fingerprinted report with observation and simulation limits |

## Object shapes

`SecurityEvent`: `event_id`, `event_type` (`activity|zone_entry|dwell|repeated_presence|sequence|multi_camera`, plus health events `camera_offline|camera_frozen|camera_ai_failed|camera_stalled|camera_restarting|camera_failed|camera_online|system_health`, which never belong to an incident), `severity`, `camera_id`, `zone_id`, `rule_id`, `timestamp_ms`, `started_ms`, `confidence`, `summary`, `label`, `track_ids`, `site_track_id`, `reason_codes`, `details`, `incident_id`, `snapshot_path`, `clip_path`.

`SecurityEvent.event_type` also includes `scene_condition` (environmental info events). Events from categories with alerts off carry `reason_codes: ["category_alerts_off"]` and `details.original_severity`.

`Incident`: `incident_id`, `opened_ms`, `updated_ms`, `closed_ms`, `status`, `severity`, `title`, `cameras`, `zones`, `site_track_ids`, `event_ids`, `event_types`, `notified_severity`, `acknowledged_ms`, `acknowledged_by`, `resolution`, `resolved_by`, `clip_paths`, `snapshot_path`, `protected`.

`SecurityEvent.event_type` `sensor_test` is a simulated sensor's test press (`info`, `camera_id` empty, `details.device_id`).

`Notification.kind` is `incident`, `escalation`, `health` (coverage problems and recoveries; `incident_id` then holds the outage id), or `system` (for example the installer's test notification).

## Mobile app notes

- Use the same endpoints. Live updates come from SSE, or from MQTT where a broker is reachable.
- Push delivery today is ntfy (self-host for production). A native app would register for APNs/FCM through a future `/api/v1/push-clients` endpoint (Phase 3; `/api/v1/devices` is the hardware registry), with push payloads carrying only the incident id and title. The app fetches details over the authenticated API.
- Treat `summary` and `title` as display text. Do not parse them; use `event_type`, `reason_codes`, and `details`.
