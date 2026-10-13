# Sentinel

Sentinel is a local-first, multi-camera security system for homes and small businesses. It aims for **continuous situational awareness, not motion alerts**. It tracks people and vehicles over time and across cameras, recognizes sequences of behaviour inside your property zones, groups them into incidents, and notifies you only when something is worth a look. A drone can later serve as a mobile camera, behind operator approval.

Sentinel describes observed activity ("Person entered Shed", "Person moved Driveway -> Side gate -> Backyard"). It never claims to know who someone is or what they intend.

- Developer guide (authoritative): [CLAUDE.md](./CLAUDE.md)
- Roadmap: [ROADMAP.md](./ROADMAP.md)
- Architecture and event processing: [docs/architecture.md](./docs/architecture.md)
- API for the dashboard and mobile apps: [docs/api.md](./docs/api.md)
- Drone integration and safety gates: [docs/drone-integration.md](./docs/drone-integration.md)
- Real-footage benchmarking: [docs/benchmarking.md](./docs/benchmarking.md)
- License audit: [sources.md](./sources.md)

## Quick start (no cameras needed)

```powershell
pip install -r requirements.txt
$env:PYTHONPATH='src'
python -m sentinel serve
```

Open http://127.0.0.1:8080. The example site runs three simulated cameras on a 4-minute loop: a parked car, a delivery (ordinary activity, no alert), and a person walking from the driveway past the side gate into the backyard and the shed. The last one becomes a single high incident with a clip, a snapshot, a push notification (logged to the console unless a channel is enabled), and a drone inspection request waiting for your approval on the Drone tab.

Replay a scenario offline in a few seconds:

```powershell
python -m sentinel replay --scenario config/scenarios/prowler.json --at 2026-10-12T02:00:00
python -m sentinel replay --scenario config/scenarios/delivery.json --arm-mode away
```

Run the tests:

```powershell
python -m unittest discover -s tests
```

## Connecting real cameras

1. Copy the example config to an ignored local file: `Copy-Item config/sentinel.example.json config/sentinel.local.json`.
2. For each camera, set the source. Prefer the camera's low-resolution sub-stream. Keep passwords in the environment:
   ```json
   "source": {"type": "rtsp", "url": "rtsp://viewer:${CAM_DRIVEWAY_PASS}@192.168.1.20:554/stream2"}
   ```
   USB webcams use `{"type": "usb", "index": 0}`. Recorded files use `{"type": "file", "path": "clip.mp4"}`.
3. Set `"detector": {"type": "mediapipe"}`. The EfficientDet-Lite model (about 7 MB) downloads to `models/` on first run.
4. Draw zones as normalized polygons over each camera view (0–1 coordinates; the ground point of a person is the bottom-centre of their box). Add `privacy_masks` over neighbours' windows or the street, and fill in `topology.links` with realistic walking times between cameras.
5. Validate the config, then run with a token:
   ```powershell
   python -m sentinel validate --config config/sentinel.local.json
   $env:SENTINEL_API_TOKEN = '<long random string>'
   $env:CAM_DRIVEWAY_PASS = '<camera password>'
   python -m sentinel serve --config config/sentinel.local.json
   ```
6. Push notifications: enable the `ntfy` channel and set `SENTINEL_NTFY_TOPIC` to a long, unguessable topic (or self-host ntfy). Start in `home` mode (only high-severity pushes) and review incidents for a week before switching to `away`.

**Security:** never port-forward Sentinel or your cameras. For remote access, use a VPN (Tailscale/WireGuard) or an authenticated HTTPS reverse proxy. Without `SENTINEL_API_TOKEN`, the server refuses to listen on anything except localhost.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/sentinel/` | Sentinel: ingestion, detection, tracking, temporal engine, incidents, API, dashboard, drone |
| `config/sentinel.example.json`, `config/scenarios/` | Demo site and scripted scenarios |
| `tests/test_sentinel_*.py` | Sentinel tests |
| `src/senior_safety/`, `webapp/`, `scripts/`, `integrations/`, `data/` | Legacy CameraAI senior-safety project (frozen; see [docs/history/senior-night-safety/](./docs/history/senior-night-safety/)) |

## Status

The security brain works end to end and is covered by tests. It has been measured on real, publicly annotated footage, including night, indoor, storefront, and multi-camera load runs (see the findings in [ROADMAP.md](./ROADMAP.md)). The headline result: detection precision is high, but recall with the default CPU model is too low for unattended use. Improving detection on real cameras is the current priority. The drone layer is simulation-only. The MAVLink adapter reads telemetry, and every flight command is disabled by design.
