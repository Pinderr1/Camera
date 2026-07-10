import csv
import copy
import tempfile
import unittest
from pathlib import Path

from senior_safety.clock import make_tick
from senior_safety.mqtt_bridge import LiveSession, alert_topic, normalize_payload
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

    def test_external_notification_topic_requires_explicit_opt_in(self):
        self.assertEqual(alert_topic("senior-night", False), "senior-night/alerts/observed")
        self.assertEqual(alert_topic("senior-night", True), "senior-night/alerts")

    def test_loaded_sensor_inventory_rejects_unknown_sensor_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(
                machine=NightSafetyStateMachine(RULES),
                log_dir=Path(tmp),
                allowed_sensor_ids={"bed_01"},
            )

            with self.assertRaisesRegex(ValueError, "unknown sensor_id: intruder_01"):
                session.handle_payload(payload("bed_occupied", False, 1_000, sensor_id="intruder_01"))

    def test_alert_payload_has_durable_context(self):
        alerts = []
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(
                machine=NightSafetyStateMachine(RULES),
                log_dir=Path(tmp),
                on_alert=alerts.append,
            )
            session.handle_payload(payload("bed_occupied", False, 1_000, event_id="bed_exit_1"))
            session.handle_event(make_tick(902_000))

            self.assertTrue((Path(tmp) / "alert_state.json").exists())

        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["alert_id"].startswith("alert_902000_"))
        self.assertEqual(alerts[0]["alert_timestamp_ms"], 902_000)
        self.assertEqual(alerts[0]["last_zone"], "bed_zone")
        self.assertEqual(alerts[0]["sensor_health"], "ok")
        self.assertEqual(alerts[0]["incident_age_s"], 901.0)
        self.assertEqual(alerts[0]["target"], "primary")

    def test_unacknowledged_urgent_alert_escalates_to_backup_then_all_caregivers(self):
        rules = copy.deepcopy(RULES)
        rules["alert_ladder"]["primary_ack_timeout_s"] = 1
        rules["alert_ladder"]["backup_ack_timeout_s"] = 2
        alerts = []
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(
                machine=NightSafetyStateMachine(rules),
                log_dir=Path(tmp),
                on_alert=alerts.append,
            )
            session.handle_payload(
                payload("manual_help_pressed", True, 1_000, event_id="help_1", sensor_id="help_01")
            )
            session.handle_event(make_tick(2_001))
            session.handle_event(make_tick(4_002))

        self.assertEqual(
            [item["event_type"] for item in alerts],
            ["alert_opened", "alert_escalated", "alert_unacknowledged"],
        )
        self.assertEqual([item["target"] for item in alerts], ["primary", "backup", "all_caregivers"])
        self.assertTrue(all(item["automatic_emergency_calls_enabled"] is False for item in alerts))

    def test_acknowledgement_is_logged_and_stops_escalation(self):
        rules = copy.deepcopy(RULES)
        rules["alert_ladder"]["primary_ack_timeout_s"] = 1
        alerts = []
        statuses = []
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(
                machine=NightSafetyStateMachine(rules),
                log_dir=Path(tmp),
                on_alert=alerts.append,
                on_alert_status=statuses.append,
            )
            session.handle_payload(
                payload("manual_help_pressed", True, 1_000, event_id="help_1", sensor_id="help_01")
            )
            alert_id = alerts[0]["alert_id"]
            decision = session.handle_payload(
                payload(
                    "caregiver_acknowledged",
                    True,
                    1_500,
                    event_id="ack_1",
                    alert_id=alert_id,
                    actor_id="caregiver_primary",
                )
            )
            session.handle_event(make_tick(3_000))

            lifecycle_files = list(Path(tmp).glob("alert_lifecycle_*.csv"))
            with lifecycle_files[0].open(newline="", encoding="utf-8") as handle:
                lifecycle_rows = list(csv.DictReader(handle))

        self.assertEqual(decision.reason_codes, ["caregiver_acknowledged"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(statuses[0]["acknowledged_by"], "caregiver_primary")
        self.assertEqual(statuses[0]["acknowledged_at_ms"], 1_500)
        self.assertEqual([row["event_type"] for row in lifecycle_rows], ["alert_opened", "alert_acknowledged"])

    def test_resolution_closes_alert_and_records_outcome(self):
        alerts = []
        statuses = []
        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(
                machine=NightSafetyStateMachine(RULES),
                log_dir=Path(tmp),
                on_alert=alerts.append,
                on_alert_status=statuses.append,
            )
            session.handle_payload(
                payload("manual_help_pressed", True, 1_000, event_id="help_1", sensor_id="help_01")
            )
            session.handle_payload(
                payload(
                    "alert_resolved",
                    True,
                    2_000,
                    event_id="resolve_1",
                    alert_id=alerts[0]["alert_id"],
                    actor_id="caregiver_primary",
                    outcome="checked_safe",
                )
            )

        self.assertEqual(statuses[0]["event_type"], "alert_resolved")
        self.assertEqual(statuses[0]["resolved_by"], "caregiver_primary")
        self.assertEqual(statuses[0]["resolution"], "checked_safe")

    def test_duplicate_event_is_not_renotified_after_restart(self):
        alerts = []
        duplicate = payload("manual_help_pressed", True, 1_000, event_id="help_duplicate", sensor_id="help_01")
        with tempfile.TemporaryDirectory() as tmp:
            first = LiveSession(
                machine=NightSafetyStateMachine(RULES),
                log_dir=Path(tmp),
                on_alert=alerts.append,
            )
            first.handle_payload(duplicate)
            restarted = LiveSession(
                machine=NightSafetyStateMachine(RULES),
                log_dir=Path(tmp),
                on_alert=alerts.append,
            )
            restarted.handle_payload(duplicate)

        self.assertEqual(len(alerts), 1)


if __name__ == "__main__":
    unittest.main()
