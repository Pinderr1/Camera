# Sentinel

Sentinel is a local-first, multi-camera security system for homes and small businesses. It aims for **continuous situational awareness, not motion alerts**. It tracks people and vehicles over time and across cameras, recognizes sequences of behaviour inside your property zones, groups them into incidents, and notifies you only when something is worth a look. A drone can later serve as a mobile camera, behind operator approval.

Sentinel describes observed activity ("Person entered Shed", "Person moved Driveway -> Side gate -> Backyard"). It never claims to know who someone is or what they intend.

- Developer guide (authoritative): [CLAUDE.md](./CLAUDE.md)
- Roadmap: [ROADMAP.md](./ROADMAP.md)
- Architecture and event processing: [docs/architecture.md](./docs/architecture.md)
- API for the dashboard and mobile apps: [docs/api.md](./docs/api.md)
- Drone integration and safety gates: [docs/drone-integration.md](./docs/drone-integration.md)
- Real-footage benchmarking: [docs/benchmarking.md](./docs/benchmarking.md)
- Running 24/7: health, recovery, background start, backups: [docs/reliability.md](./docs/reliability.md)
- Rules, explanations, activity insights, investigation: [docs/intelligence.md](./docs/intelligence.md)
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

## Installing Sentinel

The dashboard opens on the **Guide** tab until installation is finished: describe what you want ("I already have eight Hikvision cameras", "watch my driveway at night") and Sentinel AI Guide checks the system, explains what it finds and proposes changes for you to confirm. It works offline with a built-in guide; an AI provider (DeepSeek by default, or Claude) can be turned on in Guide → Assistant settings. See [docs/assistant.md](./docs/assistant.md).

Or open the dashboard's **Install** tab and pick how you are starting: connect existing cameras, install new cameras, start with a drone (no fixed cameras needed), or build a complete Sentinel package. The journey saves as you go, plans cameras on a property map with approximate coverage, registers recorders, drones, docks and sensors, and ends with a readiness summary that separates "connected", "video available" and "AI monitoring verified". See [docs/installation.md](./docs/installation.md). A drone-first demo with no cameras: `python -m sentinel serve --config config/sentinel.drone-first.example.json`.

## Connecting real cameras

The easy way: open the dashboard, go to **Setup → Cameras → Add camera** (or Install → Register and connect hardware), and follow the wizard (RTSP brand templates, DVR/NVR channels, ONVIF discovery, HTTP snapshots, USB/capture devices). Then draw zones and privacy masks on the live frame. Details and troubleshooting: [docs/camera-setup.md](./docs/camera-setup.md).

Or edit a config file by hand:

1. Copy the example config to an ignored local file: `Copy-Item config/sentinel.example.json config/sentinel.local.json`.
2. For each camera, set the source. Prefer the camera's low-resolution sub-stream. Keep passwords in the environment:
   ```json
   "source": {"type": "rtsp", "url": "rtsp://viewer:${CAM_DRIVEWAY_PASS}@192.168.1.20:554/stream2"}
   ```
   USB webcams use `{"type": "usb", "index": 0}`. Recorded files use `{"type": "file", "path": "clip.mp4"}`.
3. Set `"detector": {"type": "onnx", "model": "auto"}`. On a machine with a GPU (Windows: `onnxruntime-directml`, which works with any DirectX 12 GPU), this runs YOLOX-s at 960x544. On CPU only, it runs YOLOX-tiny at 416. Models download to `models/` on first run. Without ONNX Runtime, Sentinel falls back to EfficientDet-Lite0, which finds far fewer people (see the ROADMAP findings).
4. Draw zones as normalized polygons over each camera view (0–1 coordinates; the ground point of a person is the bottom-centre of their box). Add `privacy_masks` over neighbours' windows or the street, and fill in `topology.links` with realistic walking times between cameras.
5. Validate the config, then run with a token:
   ```powershell
   python -m sentinel validate --config config/sentinel.local.json
   $env:SENTINEL_API_TOKEN = '<long random string>'
   $env:CAM_DRIVEWAY_PASS = '<camera password>'
   python -m sentinel serve --config config/sentinel.local.json
   ```
6. Push notifications: enable the `ntfy` channel and set `SENTINEL_NTFY_TOPIC` to a long, unguessable topic (or self-host ntfy). Start in `home` mode (only high-severity pushes) and review incidents for a week before switching to `away`.

7. Run it unattended: `python -m sentinel service install --config config/sentinel.local.json --env-file <path>` starts Sentinel at boot under a supervisor that restarts it after crashes. Check the **System** tab (or `python -m sentinel service status`) for coverage, outages, and backups. Optionally set `health.heartbeat.url` to an external dead-man's switch so you hear about power cuts. See [docs/reliability.md](./docs/reliability.md).

**Security:** never port-forward Sentinel or your cameras. For remote access, use a VPN (Tailscale/WireGuard) or an authenticated HTTPS reverse proxy. Without `SENTINEL_API_TOKEN`, the server refuses to listen on anything except localhost.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/sentinel/` | Sentinel: ingestion, detection, tracking, temporal engine, incidents, API, dashboard, drone |
| `config/sentinel.example.json`, `config/scenarios/` | Demo site and scripted scenarios |
| `tests/test_sentinel_*.py` | Sentinel tests |
| `src/senior_safety/`, `webapp/`, `scripts/`, `integrations/`, `data/` | Legacy CameraAI senior-safety project (frozen; see [docs/history/senior-night-safety/](./docs/history/senior-night-safety/)) |

## Status

The security brain works end to end and is covered by tests. It has been measured on real, publicly annotated footage, including night, indoor, storefront, and multi-camera load runs (see the findings in [ROADMAP.md](./ROADMAP.md)). After the detector upgrade (YOLOX via ONNX Runtime, GPU-accelerated where available), person recall on the doorbell-height MOT16-09 clip rose from 28% to 74% at 95% precision. Crowded and heavily occluded scenes remain the weak spot (32% on MOT16-02), and nothing has been validated on real residential cameras yet. The drone layer is simulation-only. The MAVLink adapter reads telemetry, and every flight command is disabled by design.
