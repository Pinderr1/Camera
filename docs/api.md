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
| GET | `/api/v1/site` | Site map, cameras (no source URLs), zones, topology, rules, privacy |
| GET | `/api/v1/state` | Settings, camera health, open incidents, active site tracks |
| GET | `/api/v1/cameras` | Cameras with health (`connected`, `fps`, `last_frame_ms`, `error`) |
| GET | `/api/v1/cameras/{id}/snapshot.jpg` | Latest annotated frame (privacy-masked) |
| GET | `/api/v1/cameras/{id}/stream.mjpg` | MJPEG preview at the camera's fps |
| GET | `/api/v1/events` | Filters: `since_ms`, `until_ms`, `camera_id`, `min_severity`, `incident_id`, `limit` (≤1000) |
| GET | `/api/v1/incidents` | Filters: `status=open\|closed`, `since_ms`, `limit` |
| GET | `/api/v1/incidents/{id}` | Incident plus its events |
| POST | `/api/v1/incidents/{id}/acknowledge` | Idempotent |
| POST | `/api/v1/incidents/{id}/resolve` | `{"outcome": "expected_activity\|false_alarm\|real_concern\|unclear"}` |
| GET | `/api/v1/media/{path}` | Clips and snapshots referenced by events and incidents |
| GET / PATCH | `/api/v1/settings` | `arm_mode` (`disarmed\|home\|away`), `sensitivity` (`low\|normal\|high`), `notifications_enabled` |
| GET | `/api/v1/audit` | Operator and system actions |
| GET | `/api/v1/stream` | Server-sent events: `event`, `incident`, `notification`, `settings`, `drone` |
| GET | `/api/v1/drone` | Telemetry, safety state, preflight blockers, requests, geofence |
| POST | `/api/v1/drone/inspections` | `{"zone_id": "..."}` or `{"lat":..,"lon":..}`, plus optional `incident_id`. Creates a pending request; this never flies |
| POST | `/api/v1/drone/inspections/{id}/approve` | Operator approval. Runs preflight checks, then starts the mission |
| POST | `/api/v1/drone/inspections/{id}/deny` | `{"note": "..."}` |
| POST | `/api/v1/drone/commands` | `{"action": "takeoff\|goto\|hold\|return_home\|land", "params": {...}}` |

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

A **finding** is `{"code", "status", "title", "explanation", "evidence": [...], "steps": [...], "data": {...}}`. `status` is `confirmed` (Sentinel observed the fault), `suspected` (a likely cause the evidence cannot prove), `ruled_out` (checked, not the problem), `info`, or `ok`. Codes are stable identifiers (for example `unreachable`, `subnet_mismatch`, `rtsp_closed`, `vpn_route`, `public_profile`, `found_elsewhere` with `data.host`); titles and text may change. Diagnosis, discovery, and channel searches are written to the audit log (address and outcome only).

Errors use standard status codes with `{"detail": "..."}`: 401 (auth), 404 (unknown id), 409 (drone rejected, for example preflight blocked), 422 (validation).

## Object shapes

`SecurityEvent`: `event_id`, `event_type` (`activity|zone_entry|dwell|repeated_presence|sequence|multi_camera|camera_offline|camera_online`), `severity`, `camera_id`, `zone_id`, `rule_id`, `timestamp_ms`, `started_ms`, `confidence`, `summary`, `label`, `track_ids`, `site_track_id`, `reason_codes`, `details`, `incident_id`, `snapshot_path`, `clip_path`.

`SecurityEvent.event_type` also includes `scene_condition` (environmental info events). Events from categories with alerts off carry `reason_codes: ["category_alerts_off"]` and `details.original_severity`.

`Incident`: `incident_id`, `opened_ms`, `updated_ms`, `closed_ms`, `status`, `severity`, `title`, `cameras`, `zones`, `site_track_ids`, `event_ids`, `event_types`, `notified_severity`, `acknowledged_ms`, `acknowledged_by`, `resolution`, `resolved_by`, `clip_paths`, `snapshot_path`.

## Mobile app notes

- Use the same endpoints. Live updates come from SSE, or from MQTT where a broker is reachable.
- Push delivery today is ntfy (self-host for production). A native app would register for APNs/FCM through a future `/api/v1/devices` endpoint (Phase 3), with push payloads carrying only the incident id and title. The app fetches details over the authenticated API.
- Treat `summary` and `title` as display text. Do not parse them; use `event_type`, `reason_codes`, and `details`.
