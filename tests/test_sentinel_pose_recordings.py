"""Public synthetic fixtures only; private cam01 files are never required."""

import json
from pathlib import Path
import tempfile
import unittest

from sentinel.bench.pose import PoseRecordingScenario
from sentinel.replay import run_scenario
from scripts.audit_pose_recordings import digest, inspect_recording
from tests.sentinel_helpers import make_config


def row(timestamp, present=True, out_of_frame=False):
    points = {"left_shoulder": (0.4, 0.2), "right_shoulder": (0.6, 0.2),
              "left_hip": (0.4, 0.5), "right_hip": (0.6, 0.5),
              "left_ankle": (0.4, 1.2 if out_of_frame else 0.9), "right_ankle": (0.6, 0.9)}
    return {"timestamp_ms": timestamp, "clip_id": "synthetic_test", "landmarks": [
        {"name": name, "x": x, "y": y, "visibility": 0.95} for name, (x, y) in points.items()
    ] if present else []}


class PoseRecordingTests(unittest.TestCase):
    def scenario(self, rows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "synthetic.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return PoseRecordingScenario(path, "cam_a")

    def test_gaps_and_absent_samples_do_not_create_people(self):
        scenario = self.scenario([row(1000), row(1100, False), row(2000)])
        self.assertEqual(len(scenario.detections_at("cam_a", 0)), 1)
        self.assertEqual(scenario.detections_at("cam_a", 0.1), [])
        self.assertEqual(scenario.detections_at("cam_a", 0.8), [])
        self.assertEqual(scenario.detections_at("other", 0), [])
        self.assertEqual(scenario.detections_at("cam_a", -0.1), [])
        self.assertEqual(scenario.detections_at("cam_a", 1.1), [])
        self.assertEqual(self.scenario([]).detections_at("cam_a", 0), [])

    def test_sparse_capture_is_not_filled_through_long_gap(self):
        scenario = self.scenario([row(1000), row(2000)])
        self.assertEqual(scenario.detections_at("cam_a", 0.5), [])

    def test_out_of_frame_landmarks_are_clipped(self):
        box = self.scenario([row(1000, out_of_frame=True)]).detections_at("cam_a", 0)[0].bbox
        self.assertAlmostEqual(box[1] + box[3], 1.0)

    def test_recorded_observations_reach_real_sentinel_activity_engine(self):
        scenario = self.scenario([row(1000 + i * 100) for i in range(51)])
        result = run_scenario(make_config(), scenario, arm_mode="disarmed", tail_s=2)
        self.assertTrue(any(e["event_type"] == "activity" for e in result["events"]))
        self.assertEqual(result["incidents"], [])
        self.assertEqual(result["notifications"], [])

    def test_catalog_keeps_sensitive_fields_local_and_does_not_rewrite_source(self):
        # Use a separate fixture with private-looking values, never actual data.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            sample = row(1000)
            sample["stream_url"] = "rtsp://example.invalid/test"
            path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
            before = digest(path)
            record = inspect_recording(path)
            self.assertEqual(record["unexpected_row_keys"], ["stream_url"])
            self.assertEqual(record["privacy_pattern_hits"]["url"], 1)
            self.assertEqual(record["privacy_pattern_hits"]["secret_key"], 1)
            self.assertFalse(record["training_eligible"])
            self.assertEqual(record["primary_label"], "uncertain")
            self.assertEqual(digest(path), before)


if __name__ == "__main__":
    unittest.main()
