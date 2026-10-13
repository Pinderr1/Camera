"""End-to-end replays of the example site through tracker, engine, incidents,
alert policy and storage. These are the behavioural acceptance tests for the
Sentinel brain: ordinary visits stay quiet, concerning sequences escalate, and
uncertain identities are never merged."""

import copy
import os
import unittest
from datetime import datetime
from unittest import mock

from senior_safety.config_validation import ConfigValidationError
from sentinel.config import load_config, redact_url, resolve_env, validate_config
from sentinel.replay import run_scenario
from sentinel.simulation import Scenario

CONFIG = load_config("config/sentinel.example.json", resolve_secrets=False)
NIGHT = datetime(2026, 10, 12, 2, 0, 0)
DAY = datetime(2026, 10, 12, 14, 0, 0)


def replay(name, start, arm_mode=None):
    return run_scenario(copy.deepcopy(CONFIG), Scenario.load(f"config/scenarios/{name}.json"), start=start, arm_mode=arm_mode)


class ScenarioTests(unittest.TestCase):
    def test_delivery_is_ordinary_activity(self):
        result = replay("delivery", DAY, arm_mode="away")
        self.assertEqual(result["incidents"], [])
        self.assertEqual(result["notifications"], [])
        types = {event["event_type"] for event in result["events"]}
        self.assertEqual(types, {"activity"})
        couriers = [site for site in result["site_tracks"] if site["label"] == "person"]
        self.assertEqual(len(couriers), 1, "courier should stay one subject across driveway and porch cameras")
        self.assertEqual(couriers[0]["cameras"], ["cam_driveway", "cam_front"])

    def test_night_prowler_escalates_to_single_high_incident(self):
        result = replay("prowler", NIGHT, arm_mode="home")
        self.assertEqual(len(result["incidents"]), 1)
        incident = result["incidents"][0]
        self.assertEqual(incident["severity"], "high")
        self.assertEqual(set(incident["cameras"]), {"cam_driveway", "cam_front", "cam_backyard"})
        self.assertTrue({"sequence", "multi_camera", "zone_entry"} <= set(incident["event_types"]))
        self.assertEqual(len(result["notifications"]), 1, "one incident, one push")
        self.assertEqual(result["notifications"][0]["severity"], "high")
        shed = [event for event in result["events"] if event["zone_id"] == "shed"]
        self.assertEqual(shed[0]["severity"], "high")

    def test_prowler_while_home_in_daytime_still_flags_restricted_zone(self):
        result = replay("prowler", DAY, arm_mode="home")
        after_hours = [event for event in result["events"] if event["rule_id"] == "backyard_after_hours"]
        self.assertEqual(after_hours, [])
        self.assertEqual([n["severity"] for n in result["notifications"]], ["high"])

    def test_disarmed_records_but_never_notifies(self):
        result = replay("prowler", NIGHT, arm_mode="disarmed")
        self.assertEqual(result["notifications"], [])
        self.assertEqual(len(result["incidents"]), 1)

    def test_ambiguous_handoff_is_not_merged(self):
        result = replay("ambiguous_handoff", DAY)
        self.assertGreaterEqual(result["ambiguous_links"], 1)
        front = [site for site in result["site_tracks"] if "cam_front" in site["cameras"]]
        self.assertEqual(len(front), 1)
        self.assertEqual(front[0]["cameras"], ["cam_front"])
        self.assertTrue(front[0]["ambiguous_with"])


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        validate_config(CONFIG)

    def test_rejects_literal_camera_password(self):
        config = copy.deepcopy(CONFIG)
        config["cameras"][0]["source"] = {"type": "rtsp", "url": "rtsp://admin:hunter2@192.168.1.20/stream"}
        with self.assertRaises(ConfigValidationError) as raised:
            validate_config(config)
        self.assertIn("literal password", str(raised.exception))

    def test_rejects_unknown_zone_references_and_bad_rules(self):
        config = copy.deepcopy(CONFIG)
        config["rules"].append({"id": "bad", "type": "dwell", "zones": ["nowhere"], "severity": "extreme"})
        config["topology"]["links"].append({"from": "cam_driveway", "to": "cam_missing", "max_transit_s": 10})
        with self.assertRaises(ConfigValidationError) as raised:
            validate_config(config)
        message = str(raised.exception)
        for fragment in ("unknown zone 'nowhere'", "severity", "min_dwell_s", "unknown camera 'cam_missing'"):
            self.assertIn(fragment, message)

    def test_env_substitution_and_redaction(self):
        with mock.patch.dict(os.environ, {"CAM_PASS": "s3cret"}):
            resolved = resolve_env({"url": "rtsp://admin:${CAM_PASS}@10.0.0.5/s", "off": {"enabled": False, "topic": "${MISSING}"}})
        self.assertEqual(resolved["url"], "rtsp://admin:s3cret@10.0.0.5/s")
        self.assertEqual(redact_url(resolved["url"]), "rtsp://***@10.0.0.5/s")
        with self.assertRaises(ConfigValidationError):
            resolve_env({"topic": "${DEFINITELY_NOT_SET_SENTINEL}"})


if __name__ == "__main__":
    unittest.main()
