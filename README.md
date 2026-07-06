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
- Standard-library Python state machine under [src/senior_safety/](./src/senior_safety/).
- Offline runner for sensor-event CSVs and OmniFall-like segment exports.
- Unit tests covering normal trip, overstay, fall/no-motion, no-return, manual help, and sensor offline.

## Safety Note

This is an assistive monitor, not proof that someone is alive or medically okay. It should trigger caregiver checks for concerning patterns. It should not diagnose breathing, injury, death, stroke, heart attack, or any medical condition.

Do not commit raw video, identifiable images, consent forms, private contact details, or production secrets to git.
