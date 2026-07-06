import csv
import tempfile
import unittest
from pathlib import Path

from senior_safety.mqtt_bridge import LiveSession, normalize_payload
from senior_safety.state_machine import NightSafetyStateMachine, load_rules

RULES = load_rules("config/monitoring-rules.example.json")


def payload(event_name, value, timestamp_ms, **extra):
    return {
        "sensor_id": "bed_01",
        "sensor_type": "bed_pressure",
        "room": "bedroom",
        "zone_id": "bed_zone",
        "event_name": event_name,
        "value": value,
        "timestamp_ms": timestamp_ms,
        "event_time_local": "2026-07-06T02:00:00",
        **extra,
    }


class LiveSessionTests(unittest.TestCase):
    def test_events_produce_state_updates_transitions_and_alerts(self):
        states = []
        alerts = []
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(
                machine=NightSafetyStateMachine(RULES),
                log_dir=Path(tmp),
                on_state=states.append,
                on_alert=alerts.append,
            )
            session.handle_payload(payload("bed_occupied", False, 0))
            session.handle_payload(payload("manual_help_pressed", True, 5_000, sensor_id="help_01", sensor_type="button"))

            transitions_files = list(Path(tmp).glob("transitions_*.csv"))
            decisions_files = list(Path(tmp).glob("decisions_*.csv"))
            self.assertEqual(len(transitions_files), 1)
            self.assertEqual(len(decisions_files), 1)
            with transitions_files[0].open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual([row["to_state"] for row in rows], ["bed_exit", "urgent_alert"])
        self.assertEqual(states[-1]["state"], "urgent_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "urgent")

    def test_normalize_payload_defaults(self):
        event = normalize_payload({"event_name": "bed_occupied", "value": "off"}, now_ms=42_000)
        self.assertEqual(event.timestamp_ms, 42_000)
        self.assertIs(event.value, False)
        self.assertEqual(event.sensor_id, "unknown")


if __name__ == "__main__":
    unittest.main()
