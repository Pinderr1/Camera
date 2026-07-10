import csv
import json
import tempfile
import unittest
from pathlib import Path

from senior_safety.pose_extractor import load_zones_config
from senior_safety.pose_replay import collect_pose_paths, read_pose_jsonl, run_replay
from senior_safety.state_machine import load_rules

ZONES_CONFIG = load_zones_config("config/zones.example.json")
RULES = load_rules("config/monitoring-rules.example.json")


def person_landmarks(hip_x, hip_y, fallen=False, visibility=0.9):
    if fallen:
        points = {
            "nose": (hip_x + 0.32, hip_y),
            "left_shoulder": (hip_x + 0.22, hip_y + 0.01),
            "right_shoulder": (hip_x + 0.18, hip_y - 0.01),
            "left_hip": (hip_x + 0.02, hip_y),
            "right_hip": (hip_x - 0.02, hip_y),
            "left_knee": (hip_x - 0.12, hip_y + 0.02),
            "right_knee": (hip_x - 0.10, hip_y + 0.02),
            "left_ankle": (hip_x - 0.18, hip_y + 0.02),
            "right_ankle": (hip_x - 0.16, hip_y + 0.02),
        }
    else:
        points = {
            "nose": (hip_x, hip_y - 0.35),
            "left_shoulder": (hip_x + 0.05, hip_y - 0.25),
            "right_shoulder": (hip_x - 0.05, hip_y - 0.25),
            "left_hip": (hip_x + 0.02, hip_y),
            "right_hip": (hip_x - 0.02, hip_y),
            "left_knee": (hip_x + 0.02, hip_y + 0.15),
            "right_knee": (hip_x - 0.02, hip_y + 0.15),
            "left_ankle": (hip_x + 0.02, hip_y + 0.30),
            "right_ankle": (hip_x - 0.02, hip_y + 0.30),
        }
    return [
        {"name": name, "x": round(x, 4), "y": round(y, 4), "z": 0.0, "visibility": visibility}
        for name, (x, y) in points.items()
    ]


def write_jsonl(path, clip_id, frames):
    with path.open("w", encoding="utf-8") as handle:
        for index, (timestamp_ms, landmarks) in enumerate(frames):
            handle.write(
                json.dumps(
                    {
                        "clip_id": clip_id,
                        "frame_index": index,
                        "timestamp_ms": timestamp_ms,
                        "landmarks": landmarks,
                    }
                )
                + "\n"
            )


def fall_clip_frames():
    frames = [(t, person_landmarks(0.6, 0.40)) for t in range(0, 1100, 100)]
    frames.append((1100, person_landmarks(0.6, 0.52)))
    frames.append((1200, person_landmarks(0.6, 0.64)))
    frames.extend((t, person_landmarks(0.65, 0.75, fallen=True)) for t in range(1300, 70100, 100))
    return frames


def walking_clip_frames():
    frames = []
    for t in range(0, 12000, 100):
        offset = 0.005 if (t // 100) % 2 == 0 else -0.005
        frames.append((t, person_landmarks(0.6, 0.5 + offset)))
    return frames


def write_labels(labels_dir, fall_clip_id, walk_clip_id):
    labels_dir.mkdir(parents=True, exist_ok=True)
    with (labels_dir / "clips.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["clip_id", "primary_label", "scenario"])
        writer.writerow([fall_clip_id, "fall", "fall_side"])
        writer.writerow([walk_clip_id, "no_fall", "bed_exit_normal"])
    with (labels_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["event_id", "clip_id", "event_label", "start_ms", "impact_ms", "end_ms", "confidence", "notes"])
        writer.writerow(["ev1", fall_clip_id, "impact_or_floor_contact", 1100, 1300, "", 1.0, ""])


class PoseReplayTests(unittest.TestCase):
    def test_read_pose_jsonl_orders_frames_and_reads_landmarks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.jsonl"
            frames = [(200, person_landmarks(0.6, 0.5)), (100, person_landmarks(0.6, 0.5)), (300, [])]
            write_jsonl(path, "clip_a", frames)
            recording = read_pose_jsonl(path)

            self.assertEqual(recording.clip_id, "clip_a")
            self.assertEqual([t for t, _ in recording.frames], [100, 200, 300])
            self.assertIn("left_hip", recording.frames[0][1])
            self.assertEqual(recording.frames[2][1], {})

    def test_replay_scores_fall_and_no_fall_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pose_dir = tmp / "pose"
            pose_dir.mkdir()
            write_jsonl(pose_dir / "fall_001.jsonl", "fall_001", fall_clip_frames())
            write_jsonl(pose_dir / "walk_001.jsonl", "walk_001", walking_clip_frames())
            write_labels(tmp / "labels", "fall_001", "walk_001")

            metrics = run_replay(
                collect_pose_paths([str(pose_dir)]),
                ZONES_CONFIG,
                RULES,
                tmp / "labels",
                tmp / "out",
            )

            self.assertEqual(metrics["fall_clips"], 1)
            self.assertEqual(metrics["no_fall_clips"], 1)
            self.assertEqual(metrics["fall_recall"], 1.0)
            self.assertEqual(metrics["false_positive_clips"], 0)
            self.assertEqual(metrics["urgent_true_clips"], 1)
            self.assertEqual(metrics["urgent_false_clips"], 0)
            self.assertLessEqual(metrics["detect_latency_p50_s"], 10)
            self.assertLessEqual(metrics["urgent_latency_p95_s"], 90)

            predictions = list(csv.DictReader((tmp / "out" / "predictions.csv").read_text(encoding="utf-8").splitlines()))
            outcomes = {row["clip_id"]: row["outcome"] for row in predictions}
            self.assertEqual(outcomes, {"fall_001": "true_positive", "walk_001": "true_negative"})
            self.assertTrue((tmp / "out" / "metrics.json").exists())
            self.assertTrue((tmp / "out" / "false_positives.csv").exists())
            self.assertTrue((tmp / "out" / "false_negatives.csv").exists())


if __name__ == "__main__":
    unittest.main()
