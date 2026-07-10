# Event And Detector Schemas

The state machine consumes normalized events from Home Assistant, ESPHome, MQTT, OmniFall replay, or pose extraction. Events should be sorted by `timestamp_ms`.

## Normalized Input Event

```json
{
  "event_id": "evt_001",
  "sensor_id": "bed_01",
  "sensor_type": "bed_pressure",
  "room": "bedroom",
  "zone_id": "bed_zone",
  "timestamp_ms": 1000,
  "event_time_local": "2026-07-06T23:15:00",
  "event_name": "bed_occupied",
  "value": false,
  "confidence": 0.98,
  "battery_ok": true,
  "network_ok": true,
  "notes": ""
}
```

Caregiver lifecycle events use the same envelope and add `alert_id`, `actor_id`,
and (for resolution) `outcome`:

```json
{
  "event_id": "ack_001",
  "sensor_id": "caregiver_phone",
  "sensor_type": "caregiver_action",
  "timestamp_ms": 10000,
  "event_name": "caregiver_acknowledged",
  "value": true,
  "alert_id": "alert_1000_8f15b7c921",
  "actor_id": "caregiver_primary",
  "outcome": ""
}
```

Use `alert_resolved` with an outcome such as `checked_safe`, `checked_real`,
`false_alarm`, `nuisance`, or `uncertain` after the caregiver completes the check.
Acknowledgement stops backup escalation but does not resolve the alert.

## Detector Output

```json
{
  "timestamp_ms": 1000,
  "state": "bed_exit",
  "severity": "info",
  "score": 0.3,
  "confidence": 0.98,
  "reason_codes": ["outside_bed"],
  "recommended_action": "observe",
  "suppressions": [],
  "debug": {
    "bed_unoccupied_s": 0.0,
    "bathroom_occupied_s": 0.0,
    "fall_suspected_s": 0.0,
    "no_motion_s": 0.0,
    "sensor_health": "ok"
  }
}
```

## Allowed States

`asleep_in_bed`, `bed_exit`, `walking_to_bathroom`, `bathroom_occupied`, `returning_to_bed`, `returned_to_bed`, `out_of_bed_unknown`, `possible_fall`, `fallen_no_motion`, `bathroom_overstay`, `unusual_inactivity`, `needs_check`, `urgent_alert`, `offline_or_blind`.

## Implementation Rule

The event normalizer may adapt upstream names, but the state machine should only receive these normalized event names. That keeps Home Assistant, OmniFall replay, pose extraction, and future classifiers interchangeable.

Caregiver lifecycle events are intercepted by the bridge before detector
processing. Supported lifecycle names are `caregiver_acknowledged` and
`alert_resolved`.

When the bridge loads a sensor inventory, ordinary events whose `sensor_id` is
not present in that inventory are rejected before detector processing. Tick and
caregiver lifecycle events are handled separately.

## Alert Delivery Output

Each delivered alert has a restart-stable `alert_id`, `alert_timestamp_ms`,
`last_zone`, `sensor_health`, `incident_age_s`, `target`, and
`escalation_stage`. Urgent alerts start at `target=primary`, move to
`target=backup` after `primary_ack_timeout_s`, and move to
`target=all_caregivers` after the backup timeout if still unacknowledged.
Lifecycle status is published separately on `senior-night/alerts/status`.
Automatic emergency calling remains disabled.
