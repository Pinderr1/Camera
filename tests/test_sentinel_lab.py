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
        self.assertEqual(view["scene"]["preset"], "residential")  # no saved scene: the clip's default preset
        self.assertEqual([zone["type"] for zone in view["zones"]], ["yard", "entrance"])
        types = {event["type"] for event in view["events"]}
        self.assertIn("zone_entry", types, view["events"])
        entry = next(event for event in view["events"] if event["type"] == "zone_entry")
        self.assertEqual((entry["severity"], entry["explain"]["rule"]), ("low", "Person at the entrance (daytime)"))
        self.assertIn("below the away mode alert level", entry["explain"]["alert_reason"])  # a daytime visit is logged, not pushed
        for event in view["events"]:
            for word in ("intruder", "suspicious", "burglar"):
                self.assertNotIn(word, (event["summary"] or "").lower())
        presence = view["summary"]["presence"]
        self.assertEqual(presence["found"], presence["present_frames"])  # the fake person is always there
        self.assertGreater(presence["false_positive_frames"], 0)  # ... including in the 'absent' range
        self.assertTrue(view["summary"]["errors"])
        self.assertTrue(all(alert["simulated"] for alert in view["alerts"]))
        self.assertEqual(view["alerts"], [])

    def retail(self, **zones):
        body = {"preset": "retail", "zones": [{"name": "Shop floor", "type": "public", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}
        if zones.get("restricted"):
            body["zones"].append({"name": "Stock room", "type": "restricted", "polygon": zones["restricted"]})
        return self.manager.save_scene("own_front_door", body, "test")

    def run_at(self, start_time=None):
        with mock.patch("sentinel.detectors.create_detector", return_value=WalkingPerson()):
            return self.manager.run_blocking("own_front_door", profile="fake", fps=10, species=False, max_seconds=10, start_time=start_time)

    def test_retail_shop_floor_is_ordinary_activity(self):
        self.retail()
        view = self.run_at()
        self.assertEqual(view["state"], "done", view["error"])
        self.assertEqual(view["scene"]["source"], "saved")
        self.assertEqual(view["alerts"], [])
        self.assertEqual({event["severity"] for event in view["events"]}, {"info"}, view["events"])  # logged, never pushed
        activity = next(event for event in view["events"] if event["type"] == "activity")
        self.assertTrue(activity["explain"]["ordinary_area"])
        self.assertEqual(activity["explain"]["alert"], "logged")
        self.assertTrue(self.manager.get(view["id"]).frame_records()[5]["dets"])  # detections are still logged

    def test_retail_restricted_area_and_after_hours_alert_with_explanation(self):
        self.retail(restricted=[[0.3, 0.6], [0.7, 0.6], [0.7, 1], [0.3, 1]])
        view = self.run_at()
        entry = next(event for event in view["events"] if event["type"] == "zone_entry")
        explain = entry["explain"]
        self.assertEqual((entry["severity"], explain["zone"], explain["zone_type"]), ("high", "Stock room", "restricted"))
        self.assertEqual(explain["rule"], "Person entered a staff-only area")
        self.assertIn("restricted", explain["rule_what"])
        self.assertGreaterEqual(explain["observations"], 3)
        self.assertIsNotNone(explain["duration_s"])
        self.assertGreater(explain["confidence"], 0.5)
        self.assertEqual(explain["alert"], "alerted")
        self.assertEqual(len(view["alerts"]), 1)
        self.retail()
        night = self.run_at("23:30")  # same walk with the shop closed
        self.assertEqual(night["options"]["start_time"], "23:30")
        rules = {event["explain"]["rule"] for event in night["events"] if event["severity"] == "high"}
        self.assertEqual(rules, {"Person in the shop after hours"})
        self.assertEqual(len(night["alerts"]), 1)
        for text in [a["title"] + (a["body"] or "") for a in view["alerts"] + night["alerts"]] + [e["summary"] for e in night["events"]]:
            for word in ("intruder", "suspicious", "burglar", "thief", "criminal", "shoplift"):
                self.assertNotIn(word, text.lower())

    def test_scenes_are_validated_and_stay_in_the_lab(self):
        from sentinel.lab import scenes

        with self.assertRaises(ValueError):
            self.manager.save_scene("own_front_door", {"preset": "casino", "zones": []}, "test")
        with self.assertRaises(ValueError):
            self.manager.save_scene("own_front_door", {"preset": "retail", "zones": [{"name": "x", "type": "public", "polygon": [[0, 0], [2, 0], [1, 1]]}]}, "test")
        with self.assertRaises(ValueError):
            self.manager.save_scene("own_front_door", {"preset": "retail", "zones": [{"name": "Floor", "type": "public", "polygon": [[0, 0], [1, 0], [1, 1]]}],
                                                       "rules": {"res_linger": {"enabled": False}}}, "test")  # another preset's rule
        saved = self.manager.save_scene("own_front_door", {"preset": "retail", "zones": [{"name": "Floor", "type": "public", "polygon": [[0, 0], [1, 0], [1, 1]]}],
                                                           "rules": {"ret_after_hours": {"active_hours": {"start": "21:00", "end": "08:00"}}}}, "test")
        after = next(rule for rule in saved["rule_list"] if rule["id"] == "ret_after_hours")
        self.assertEqual(after["active_hours"], {"start": "21:00", "end": "08:00"})
        self.assertTrue((self.storage / "lab" / "scenes" / "own_front_door.json").exists())
        self.assertFalse((self.storage / "site.json").exists())  # never the site config
        self.assertEqual(self.manager.reset_scene("own_front_door")["source"], "preset")
        for preset in scenes.PRESETS.values():
            for rule in preset["rules"]:
                for word in ("intruder", "suspicious", "burglar", "thief", "criminal", "loiter"):
                    self.assertNotIn(word, rule["name"].lower())
        from sentinel.config import validate_config
        from sentinel.lab.session import lab_site_config

        for key in scenes.PRESETS:  # every preset builds a valid site; public areas are `general` zones
            config = lab_site_config(catalog.LabClip("x", "x", "lab", "c", [], "day", "CC0", True, "test", "none"), {"type": "scripted"}, 5, False,
                                     self.storage, scenes.preset_scene(key))
            validate_config(config)
            self.assertNotIn("public", {zone["type"] for zone in config["cameras"][0]["zones"]})
        self.assertGreater(len(self.manager.still("own_front_door", 1.0)), 1000)

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
