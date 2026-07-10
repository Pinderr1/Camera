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

Create ignored household copies before connecting live devices:

```powershell
Copy-Item config/monitoring-rules.example.json config/monitoring-rules.local.json
Copy-Item config/sensors.example.json config/sensors.local.json
Copy-Item config/zones.example.json config/zones.local.json
```

Replace every placeholder ID and polygon, set `deployment_ready` to `true` in
all three files after household review, then run the deployment validator. It
checks safety/privacy invariants, threshold ordering, polygons, sensor IDs,
critical camera heartbeat coverage, and cross-file zone references:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.config_validation --deployment `
  --rules config/monitoring-rules.local.json `
  --sensors config/sensors.local.json `
  --zones config/zones.local.json
```

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

Run the MQTT bridge in its safe default observation mode (subscribes to
`senior-night/events`, publishes detector alerts to `senior-night/alerts/observed`,
and logs decisions, transitions, and alert lifecycle rows to `detector_runs/live/`):

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.mqtt_bridge --host <broker-ip> --critical-sensor pose_cam01
```

Only after the notification acceptance gate passes, opt in to the external
caregiver topic. Notification mode requires a validated, non-example rules,
sensors, and zones bundle:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.mqtt_bridge --host <broker-ip> --notifications-enabled `
  --rules config/monitoring-rules.local.json `
  --sensors config/sensors.local.json `
  --zones config/zones.local.json
```

Urgent alerts carry durable IDs,
escalate from primary to backup using the configured timeouts, and accept
`caregiver_acknowledged` / `alert_resolved` events on the normal events topic.
Acknowledgement and resolution status is published to
`senior-night/alerts/status`.

Run the camera pose extractor (publishes derived events over MQTT; `--dry-run` prints instead, `--show` opens a preview for zone calibration):

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.pose_extractor --zones config/zones.example.json --dry-run --show
```

Copy `config/zones.example.json` to a local config and adjust the zone polygons to your camera view before real use. **Zones that do not match the room are a top false-alarm source**: a person lying in an unzoned bed area reads as floor-level and escalates. Use `--show` to see the polygons over the live image and drag-edit the JSON until the bed zone covers the whole sleeping surface.

Calibrate the camera once after mounting it (stand fully visible, then walk the monitored path while it runs). This records your standing size and floor line so thresholds adapt to the camera distance; the config loader picks it up automatically:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.pose_extractor --zones config/zones.local.json --calibrate 30
```

The pose extractor permits example zones only with `--dry-run`; calibration and
live MQTT publishing fail closed until a household-specific file is supplied.

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

## Accuracy Evaluation

Every recorded pose JSONL can be replayed through the exact live pipeline and scored against labels in `data/labels/` (`clips.csv` + `events.csv`). Detection changes re-score old recordings, so improvements are measured, never guessed:

```powershell
$env:PYTHONPATH='src'
python -m senior_safety.pose_replay --pose data/processed/pose/cam01 --out detector_runs/replay_today --clip-decisions
```

Outputs `metrics.json` (fall recall/precision, urgent precision, latency p50/p95, FPs per hour, person coverage) plus `predictions.csv`, `false_positives.csv`, and `false_negatives.csv`.

Benchmark against the public URFD dataset (30 staged falls + 40 daily activities, downloads ~1 GB on first run, cached afterwards):

```powershell
python scripts/urfd_benchmark.py --out detector_runs/urfd_today
python scripts/urfd_benchmark.py --skip-pose --out detector_runs/urfd_rescored   # re-score cached poses after code changes
```

Sweep detection thresholds over the cached corpus and write the best operating point into the configs:

```powershell
python scripts/sweep_thresholds.py --out detector_runs/tuning
python scripts/sweep_thresholds.py --apply
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
