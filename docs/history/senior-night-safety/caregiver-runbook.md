# Caregiver Runbook

This system is an assistive monitor. It cannot prove that someone is medically okay.

## Alert Levels

- `info`: observation/logging only.
- `low`: caregiver should check when available.
- `urgent`: caregiver should check immediately.

## What To Do On An Urgent Alert

1. Check the alert state, duration, zone, reason codes, and sensor health.
2. Try a local soft check if configured.
3. Call the person.
4. If there is no answer and the context is concerning, ask the nearby keyholder or backup caregiver to check.
5. A human decides whether to call emergency services.

## Review Every Alert

Record one outcome:

- `true_positive`
- `helpful_check`
- `false_positive`
- `nuisance_alert`
- `false_negative`
- `uncertain`

Then record whether thresholds, sensors, camera placement, or alert text need to change.
