# Senior Night Safety Monitoring Prototype

This workspace is for a local-first safety monitor for an older adult who sleeps alone and may get up at night to use the washroom.

Start with [claude.md](./claude.md). It defines the product goal, safety boundaries, data plan, detection states, rule baseline, alert ladder, privacy posture, and MVP phases.

## Build Order

1. Fill out [docs/emergency-response-plan.md](./docs/emergency-response-plan.md).
2. Walk through [docs/home-setup-checklist.md](./docs/home-setup-checklist.md).
3. Review [sources.md](./sources.md) before copying or adapting any external code.
4. Start from [config/monitoring-rules.example.json](./config/monitoring-rules.example.json) and [config/sensors.example.json](./config/sensors.example.json).
5. Use [docs/architecture.md](./docs/architecture.md) and [docs/event-schema.md](./docs/event-schema.md) as the runtime contract.
6. Use [data/labels/label_schema.json](./data/labels/label_schema.json) for clips, sensor events, routine transitions, and alert reviews.
7. Run observation-only for 7 to 14 nights before waking caregivers with automated alerts.

## Setup

The state machine and offline runner are standard-library Python. The live services need:

```powershell
pip install -r requirements.txt
```

## Local Pilot (no broker needed)

The fastest way to validate the camera pipeline end to end on one machine — camera events feed the state machine directly, decisions print to the console, and logs land in `detector_runs/pilot/`:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.local_pilot --show --fast-test
```

- Set `source` in the zones config to a USB webcam index (`0`) or an IP camera URL (`"rtsp://user:pass@ip:554/stream"`).
- The MediaPipe pose model (~9 MB) downloads automatically to `models/` on first run.
- In the preview window: `q` quits, `h` presses the virtual help button, `c` cancels.
- `--fast-test` shortens thresholds (urgent after ~20 s of floor-level stillness) so staged scenarios do not require lying still for a minute.
- Review a pilot session afterwards: `python -m senior_safety.morning_review --log-dir detector_runs/pilot`.

Staged validation checklist (safe scenarios only, mats/cushions, no real falls): empty room, walk through the route zone, sit on a chair and stay still, controlled lie-down on a mat, get up again, press `h` then `c`, cover the camera. Only the lie-down and `h` should alert.

## Live Runtime

Run the MQTT bridge (subscribes to `senior-night/events`, publishes `senior-night/state` and `senior-night/alerts`, logs daily decision/transition CSVs to `detector_runs/live/`):

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.mqtt_bridge --host <broker-ip> --critical-sensor pose_cam01
```

Run the camera pose extractor (publishes derived events over MQTT; `--dry-run` prints instead, `--show` opens a preview for zone calibration):

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.pose_extractor --zones config/zones.example.json --dry-run --show
```

Copy `config/zones.example.json` to a local config and adjust the zone polygons to your camera view before real use.

Morning review of last night:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.morning_review --date 20260706 --append-review-log
```

Compute personal baselines after 7+ nights of observation (the runner and bridge pick them up automatically from `data/baselines/`):

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.baselines --transitions detector_runs/live
```

## Prototype Commands

Run the state-machine tests:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests
```

Run a normal bathroom-trip trace:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.runner --events data/examples/sensor_events_normal_trip.csv --output detector_runs/normal_trip.csv --metrics detector_runs/normal_trip.metrics.json
```

Run a fall/no-motion trace:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.runner --events data/examples/sensor_events_fall.csv --output detector_runs/fall.csv --metrics detector_runs/fall.metrics.json
```

Run an OmniFall-like segment export without duplicating OmniFall ingestion:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.runner --omnifall-segments data/examples/omnifall_segments.example.csv --output detector_runs/omnifall_replay.csv --metrics detector_runs/omnifall_replay.metrics.json
```

## Implemented Pieces

- License/reuse audit in [sources.md](./sources.md).
- Event and detector schema docs in [docs/event-schema.md](./docs/event-schema.md).
- Home Assistant and ESPHome examples under [integrations/](./integrations/).
- Standard-library Python state machine under [src/senior_safety/](./src/senior_safety/) with clock ticks, night-window gating, and alert cooldowns.
- Offline runner for sensor-event CSVs and OmniFall-like segment exports.
- Live MQTT bridge with retained state topic, alert topic, and daily decision/transition logs.
- Camera pose extractor (OpenCV + MediaPipe) emitting zone-based sensor events and pose-only JSONL.
- Personal baseline computation and morning review tooling.
- Unit tests covering normal trip, overstay, fall/no-motion, no-return, manual help, sensor offline, ticks, cooldowns, night window, pose events, and baselines.

## Safety Note

This is an assistive monitor, not proof that someone is alive or medically okay. It should trigger caregiver checks for concerning patterns. It should not diagnose breathing, injury, death, stroke, heart attack, or any medical condition.

Do not commit raw video, identifiable images, consent forms, private contact details, or production secrets to git.
