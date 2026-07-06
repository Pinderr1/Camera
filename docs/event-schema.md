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
