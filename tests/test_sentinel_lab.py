"""AI Test Lab and training workflow (fast: synthetic clip, fake detector, real runtime)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.lab import catalog  # noqa: E402
from sentinel.models import Detection  # noqa: E402

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None


class WalkingPerson:
    """A person walking from the top of the picture into 'Near the camera'."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        y = min(0.75, 0.1 + 0.03 * self.calls)
        return [Detection("person", 0.9, (0.4, y, 0.1, 0.2))]

    def close(self):
        pass


def make_clip(folder: Path, name: str = "Front door.avi", frames: int = 40) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 240))
    for i in range(frames):
        image = np.full((240, 320, 3), 40, np.uint8)
        cv2.rectangle(image, (130, 10 + i * 4), (160, 60 + i * 4), (200, 200, 200), -1)
        writer.write(image)
    writer.release()
    path.with_suffix(".labels.json").write_text(json.dumps({"present_s": [[0, 2.5]], "absent_s": [[3.0, 4.0]], "subjects": ["person"]}))
    return path


class CatalogTests(unittest.TestCase):
    def test_licences_and_splits(self):
        self.assertFalse(catalog._commercial("CC BY-NC-SA 3.0"))
        self.assertFalse(catalog._commercial("CC BY-ND 4.0"))
        self.assertTrue(catalog._commercial("Public domain"))
        self.assertTrue(catalog._commercial("CC BY-SA 2.0"))
        clips = {clip.id: clip for clip in catalog.bench_clips() + catalog._registry_clips(
            catalog.ROOT / "config/guardian/videos.json", Path("nowhere"), "guardian", "validation", "wildlife")}
        self.assertEqual(clips["mot16_09"].split, "validation")
        self.assertFalse(clips["mot16_09"].commercial_training)  # CC BY-NC-SA: evaluation only
        self.assertEqual(clips["black_bear_day"].labels, "presence")
        self.assertNotIn("path", clips["black_bear_day"].public())

    def test_lab_registry_clips_are_independent_test_data(self):
        registry = json.loads((catalog.ROOT / "config/lab/videos.json").read_text(encoding="utf-8"))
        for clip in registry["clips"]:
            self.assertEqual(clip["split"], "test", clip["id"])
            self.assertFalse(catalog._commercial(clip["licence"]) is False and "nc" in clip["licence"].lower(), clip["id"])
            self.assertTrue(clip["licence"].startswith(("Public domain", "CC BY", "CC0")), clip["id"])


@unittest.skipIf(cv2 is None, "OpenCV not installed")
class LabRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Path(self.tmp.name)
        make_clip(self.storage / "lab" / "videos")
        from sentinel.lab.session import LabManager

        self.manager = LabManager(self.storage, extra_profiles=lambda: {"fake": {"name": "Fake", "detector": {"type": "scripted"}, "requires": []}})

    def tearDown(self):
        self.tmp.cleanup()

    def run_clip(self):
        with mock.patch("sentinel.detectors.create_detector", return_value=WalkingPerson()):
            return self.manager.run_blocking("own_front_door", profile="fake", fps=10, species=False, max_seconds=10)

    def test_run_goes_through_real_rules_and_scores_labels(self):
        view = self.run_clip()
        self.assertEqual(view["state"], "done", view["error"])
        run = self.manager.get(view["id"])
        frames = run.frame_records()
        self.assertGreaterEqual(len(frames), 35)
        self.assertTrue((run.dir / "frames" / "00000.jpg").exists())
        self.assertEqual(view["clip"]["split"], "private")
        self.assertIn("lab_near", [zone["id"] for zone in view["zones"]])
        types = {event["type"] for event in view["events"]}
        self.assertIn("zone_entry", types, view["events"])
        for event in view["events"]:
            for word in ("intruder", "suspicious", "burglar"):
                self.assertNotIn(word, (event["summary"] or "").lower())
        presence = view["summary"]["presence"]
        self.assertEqual(presence["found"], presence["present_frames"])  # the fake person is always there
        self.assertGreater(presence["false_positive_frames"], 0)  # ... including in the 'absent' range
        self.assertTrue(view["summary"]["errors"])
        self.assertTrue(all(alert["simulated"] for alert in view["alerts"]))

    def test_corrections_feed_training_only_with_consent(self):
        from sentinel.training import examples, registry

        view = self.run_clip()
        with self.assertRaises(ValueError):
            self.manager.add_correction(view["id"], {"kind": "missed", "frame": 2, "bbox": [0.5, 0.5, 2, 0.1]}, "test")
        self.manager.add_correction(view["id"], {"kind": "false_positive", "frame": 35, "detection": 0}, "test")
        self.manager.add_correction(view["id"], {"kind": "missed", "frame": 3, "bbox": [0.7, 0.5, 0.1, 0.2], "label": "dog"}, "test")
        evaluation = examples.collect(self.storage, self.storage / "export_eval")
        self.assertGreater(evaluation["examples"], 2)
        training = examples.collect(self.storage, self.storage / "export_train", purpose="training")
        self.assertEqual(training["examples"], 0)  # own footage needs personalization consent
        with self.assertRaises(ValueError):
            examples.set_personalization(self.storage, True)  # who agreed must be recorded
        examples.set_personalization(self.storage, True, by="owner")
        training = examples.collect(self.storage, self.storage / "export_train2", purpose="training")
        self.assertEqual(training["coco"]["private"]["images"], 2)
        from sentinel.training.finetune import check, plan

        self.assertTrue(check(self.storage / "export_train2")["ok"])
        self.assertFalse(plan(self.storage / "export_train2")["ready"])  # far too few boxes: blocked, never trained

        models = registry.ModelRegistry(self.storage)
        candidate = self.storage / "candidate.onnx"
        candidate.write_bytes(b"not really a model")
        models.register("cand_a", candidate, family="yolox", labels="coco80", input_size=[640, 640],
                        licence="Apache-2.0", training_data="COCO + owner corrections", commercial_ok=True)
        with self.assertRaises(ValueError):
            models.register("cand_a", candidate, family="yolox", labels="coco80", input_size=[640, 640],
                            licence="Apache-2.0", training_data="x", commercial_ok=True)  # never replaced
        with self.assertRaises(ValueError):
            models.promote("cand_a", "test")  # no test-split comparison yet
        models.record_evaluation("cand_a", {"split": "test", "verdict": "no_worse"})
        self.assertEqual(models.promote("cand_a", "test")["previous"], "current")
        self.assertEqual(models.rollback("test")["active"], "current")
        candidate.write_bytes(b"changed")
        self.assertFalse(models.verify("cand_a"))

    def test_verdict_is_conservative(self):
        from sentinel.training.evaluate import verdict

        base = [{"found": 80, "of": 100, "false": 5}]
        self.assertEqual(verdict([{"found": 85, "of": 100, "false": 5}], base), "better")
        self.assertEqual(verdict([{"found": 80, "of": 100, "false": 6}], base), "no_worse")
        self.assertEqual(verdict([{"found": 90, "of": 100, "false": 20}], base), "worse")
        self.assertEqual(verdict([{"found": 0, "of": 0, "false": 0}], base), "insufficient_labels")


if __name__ == "__main__":
    unittest.main()
