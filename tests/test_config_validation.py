import copy
import unittest

from senior_safety.config_validation import (
    ConfigValidationError,
    load_sensors_config,
    reject_example_paths,
    validate_deployment_bundle,
    validate_rules,
    validate_sensors,
    validate_zones,
)
from senior_safety.pose_extractor import load_zones_config
from senior_safety.state_machine import load_rules


RULES = load_rules("config/monitoring-rules.example.json")
SENSORS = load_sensors_config("config/sensors.example.json")
ZONES = load_zones_config("config/zones.example.json")


class ConfigValidationTests(unittest.TestCase):
    def test_example_structures_are_valid(self):
        validate_rules(RULES)
        validate_sensors(SENSORS)
        validate_zones(ZONES)

    def test_household_bundle_cross_references_all_sensors_and_zones(self):
        rules = copy.deepcopy(RULES)
        sensors = copy.deepcopy(SENSORS)
        zones = copy.deepcopy(ZONES)
        for config in (rules, sensors, zones):
            config["deployment_ready"] = True
        critical = validate_deployment_bundle(
            rules,
            sensors,
            zones,
            paths=["rules.local.json", "sensors.local.json", "zones.local.json"],
        )

        self.assertIn("bed_01", critical)
        self.assertIn("pose_cam01", critical)

    def test_deployment_requires_explicit_household_review_marker(self):
        with self.assertRaisesRegex(ConfigValidationError, "deployment_ready"):
            validate_deployment_bundle(RULES, SENSORS, ZONES)

    def test_deployment_rejects_example_paths(self):
        with self.assertRaisesRegex(ConfigValidationError, "cannot use example file"):
            validate_deployment_bundle(
                RULES,
                SENSORS,
                ZONES,
                paths=["config/monitoring-rules.example.json"],
            )

    def test_live_runtime_rejects_example_path(self):
        with self.assertRaisesRegex(ConfigValidationError, "live runtime cannot use example file"):
            reject_example_paths(["config/zones.example.json"])

    def test_rules_reject_inverted_alert_thresholds(self):
        rules = copy.deepcopy(RULES)
        rules["thresholds"]["bed_exit_no_return"]["low_notice_after_s"] = 2_000
        rules["thresholds"]["bed_exit_no_return"]["urgent_after_s"] = 1_000

        with self.assertRaisesRegex(ConfigValidationError, "low_notice_after_s must be < urgent_after_s"):
            validate_rules(rules)

    def test_rules_reject_automatic_emergency_calls(self):
        rules = copy.deepcopy(RULES)
        rules["alert_ladder"]["automatic_emergency_calls_enabled"] = True

        with self.assertRaisesRegex(ConfigValidationError, "must be false for version 1"):
            validate_rules(rules)

    def test_rules_fail_closed_on_privacy_regression(self):
        rules = copy.deepcopy(RULES)
        rules["privacy"]["record_audio"] = True

        with self.assertRaisesRegex(ConfigValidationError, "privacy.record_audio: must be false"):
            validate_rules(rules)

    def test_zones_reject_out_of_frame_and_zero_area_polygons(self):
        zones = copy.deepcopy(ZONES)
        zones["zones"][0]["polygon"] = [[0.0, 0.0], [1.2, 0.0], [0.0, 0.0]]

        with self.assertRaises(ConfigValidationError) as raised:
            validate_zones(zones)

        self.assertIn("coordinates must be finite values from 0 to 1", str(raised.exception))

    def test_zones_reject_obsolete_ignored_threshold_names(self):
        zones = copy.deepcopy(ZONES)
        zones["events"]["motion_score_threshold"] = 0.1

        with self.assertRaisesRegex(ConfigValidationError, "obsolete key"):
            validate_zones(zones)

    def test_deployment_requires_bed_and_path_camera_zones(self):
        zones = copy.deepcopy(ZONES)
        zones["zones"] = [zone for zone in zones["zones"] if zone["kind"] == "doorway"]
        zones["events"]["fall_suppressed_zone_ids"] = []

        with self.assertRaises(ConfigValidationError) as raised:
            validate_zones(zones, require_camera_zones=True)

        self.assertIn("a bed zone is required", str(raised.exception))
        self.assertIn("a path zone is required", str(raised.exception))

    def test_sensors_reject_duplicates_and_unsupported_events(self):
        sensors = copy.deepcopy(SENSORS)
        sensors["sensors"][1]["sensor_id"] = sensors["sensors"][0]["sensor_id"]
        sensors["sensors"][1]["event_name"] = "made_up_event"

        with self.assertRaises(ConfigValidationError) as raised:
            validate_sensors(sensors)

        self.assertIn("duplicate ID", str(raised.exception))
        self.assertIn("unsupported event name", str(raised.exception))

    def test_bundle_rejects_unknown_sensor_zone(self):
        sensors = copy.deepcopy(SENSORS)
        sensors["sensors"][0]["zone_id"] = "bed_zoen"

        with self.assertRaisesRegex(ConfigValidationError, "unknown zone_id 'bed_zoen'"):
            validate_deployment_bundle(RULES, sensors, ZONES)

    def test_bundle_requires_camera_heartbeat_sensor(self):
        sensors = copy.deepcopy(SENSORS)
        sensors["sensors"] = [sensor for sensor in sensors["sensors"] if sensor["sensor_id"] != "pose_cam01"]

        with self.assertRaisesRegex(ConfigValidationError, "missing camera heartbeat sensor_id 'pose_cam01'"):
            validate_deployment_bundle(RULES, sensors, ZONES)

    def test_bundle_requires_critical_camera_heartbeat(self):
        sensors = copy.deepcopy(SENSORS)
        pose_sensor = next(sensor for sensor in sensors["sensors"] if sensor["sensor_id"] == "pose_cam01")
        pose_sensor["critical"] = False

        with self.assertRaisesRegex(ConfigValidationError, "must be critical and emit sensor_online"):
            validate_deployment_bundle(RULES, sensors, ZONES)


if __name__ == "__main__":
    unittest.main()
