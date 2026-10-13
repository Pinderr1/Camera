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

Errors use standard status codes with `{"detail": "..."}`: 401 (auth), 404 (unknown id), 409 (drone rejected, for example preflight blocked), 422 (validation).

## Object shapes

`SecurityEvent`: `event_id`, `event_type` (`activity|zone_entry|dwell|repeated_presence|sequence|multi_camera|camera_offline|camera_online`), `severity`, `camera_id`, `zone_id`, `rule_id`, `timestamp_ms`, `started_ms`, `confidence`, `summary`, `label`, `track_ids`, `site_track_id`, `reason_codes`, `details`, `incident_id`, `snapshot_path`, `clip_path`.

`Incident`: `incident_id`, `opened_ms`, `updated_ms`, `closed_ms`, `status`, `severity`, `title`, `cameras`, `zones`, `site_track_ids`, `event_ids`, `event_types`, `notified_severity`, `acknowledged_ms`, `acknowledged_by`, `resolution`, `resolved_by`, `clip_paths`, `snapshot_path`.

## Mobile app notes

- Use the same endpoints. Live updates come from SSE, or from MQTT where a broker is reachable.
- Push delivery today is ntfy (self-host for production). A native app would register for APNs/FCM through a future `/api/v1/devices` endpoint (Phase 3), with push payloads carrying only the incident id and title. The app fetches details over the authenticated API.
- Treat `summary` and `title` as display text. Do not parse them; use `event_type`, `reason_codes`, and `details`.
