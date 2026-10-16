"""MegaDetector V6 (Apache) as an optional animal detector next to the site's
detector: label decoding, merging, failure containment, config validation, and
(when the exported model and the wildlife sample are present) a real-model check."""

import json
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:
    np = None

from sentinel.config import ConfigValidationError as ConfigError, validate_config
from sentinel.detectors import AnimalAugmentedDetector, create_detector
from sentinel.models import Detection, canonical_class, detector_labels, effective_categories
from sentinel.pipeline import with_category_classes
from sentinel.vision import Frame

ROOT = Path(__file__).resolve().parents[1]
MD_MODEL = ROOT / "models/megadetector/mdv6_apa_rtdetr_c/model.onnx"


class StubDetector:
    def __init__(self, detections=None, error=None, providers=("CPUExecutionProvider",)):
        self.detections, self.error, self.providers, self.closed = detections or [], error, list(providers), False

    def detect(self, frame):
        if self.error:
            raise self.error
        return list(self.detections)

    def describe(self):
        return {"model": "stub"}

    def close(self):
        self.closed = True


def frame():
    return Frame(np.zeros((64, 64, 3), np.uint8) if np is not None else None, 0)


class LabelTests(unittest.TestCase):
    def test_megadetector_classes_feed_existing_categories(self):
        self.assertEqual(canonical_class("animal"), "animal")
        self.assertEqual(canonical_class("vehicle"), "vehicle")
        labels = detector_labels(effective_categories({}))
        self.assertIn("animal", labels)
        self.assertIn("vehicle", labels)
        off = detector_labels(effective_categories({"categories": {"animal": {"detect": False}}}))
        self.assertNotIn("animal", off)

    def test_registry_entry_is_licence_cleared_and_has_no_download(self):
        try:
            from sentinel.detectors_onnx import MODELS
        except ImportError:
            self.skipTest("onnxruntime not installed")
        spec = MODELS["mdv6_apa_rtdetr_c"]
        self.assertTrue(spec["commercial_ok"])
        self.assertIn("Apache-2.0", spec["license"])
        self.assertIn("LILA", spec["training_data"])
        self.assertNotIn("url", spec, "weights come from a checked export, never an automatic download")


@unittest.skipIf(np is None, "numpy not installed")
class DecodeTests(unittest.TestCase):
    def test_megadetector_outputs_decode_to_animal_person_vehicle(self):
        try:
            from test_sentinel_detectors import FakeSession, onnx_detector
        except ImportError:
            from tests.test_sentinel_detectors import FakeSession, onnx_detector
        logits = np.full((1, 300, 3), -10.0, np.float32)
        logits[0, 0, 0] = 2.0  # animal
        logits[0, 1, 1] = 2.0  # person
        logits[0, 2, 2] = 2.0  # vehicle
        boxes = np.tile(np.array([0.5, 0.5, 0.2, 0.2], np.float32), (1, 300, 1))
        boxes[0, 1] = [0.2, 0.2, 0.1, 0.1]
        boxes[0, 2] = [0.8, 0.8, 0.1, 0.1]
        session = FakeSession([1, 3, 640, 640], {"logits": logits, "pred_boxes": boxes})
        detector = onnx_detector({"model": "mdv6_apa_rtdetr_c", "classes": ["animal", "person", "vehicle"]}, session)
        detections = detector.detect(Frame(np.zeros((1536, 2048, 3), np.uint8), 0))
        self.assertEqual(sorted(d.label for d in detections), ["animal", "person", "vehicle"])
        self.assertEqual(session.fed.shape, (1, 3, 640, 640), "MegaDetector was trained on a plain 640x640 resize")
        animal = next(d for d in detections if d.label == "animal")
        self.assertAlmostEqual(animal.bbox[0], 0.4, places=4)
        json.dumps([animal.confidence, *animal.bbox])

    def test_missing_export_names_the_export_script(self):
        try:
            from sentinel import detectors_onnx
        except ImportError:
            self.skipTest("onnxruntime not installed")
        with mock.patch("pathlib.Path.exists", return_value=False), self.assertRaises(FileNotFoundError) as caught:
            detectors_onnx.OnnxDetector({"model": "mdv6_apa_rtdetr_c"}, 0.4)
        self.assertIn("export_megadetector_onnx.py", str(caught.exception))


class MergeTests(unittest.TestCase):
    def test_adds_only_animals_the_main_detector_missed(self):
        primary = StubDetector([Detection("person", 0.9, (0.1, 0.1, 0.1, 0.3)), Detection("bear", 0.7, (0.5, 0.5, 0.2, 0.2))])
        animal = StubDetector([
            Detection("animal", 0.8, (0.51, 0.5, 0.2, 0.2)),  # same bear: the COCO label is kept
            Detection("animal", 0.6, (0.0, 0.7, 0.1, 0.1)),  # missed by the main detector: added
            Detection("person", 0.9, (0.1, 0.1, 0.1, 0.3)),  # people stay the main detector's job
            Detection("vehicle", 0.9, (0.7, 0.1, 0.2, 0.1)),
        ])
        detections = AnimalAugmentedDetector(primary, animal).detect(frame())
        self.assertEqual([d.label for d in detections], ["person", "bear", "animal"])
        self.assertEqual(detections[2].bbox, (0.0, 0.7, 0.1, 0.1))

    def test_animal_detector_failure_keeps_main_detections(self):
        primary = StubDetector([Detection("person", 0.9, (0.1, 0.1, 0.1, 0.3))])
        detector = AnimalAugmentedDetector(primary, StubDetector(error=RuntimeError("DirectML device removed")))
        with self.assertLogs("sentinel.detectors", "WARNING"):
            self.assertEqual([d.label for d in detector.detect(frame())], ["person"])
        self.assertEqual(detector.animal_status[0], "degraded")
        self.assertIn("DirectML device removed", detector.animal_status[1])
        detector.animal = StubDetector([])
        detector.detect(frame())
        self.assertEqual(detector.animal_status, ("ok", ""), "recovers as soon as an inference succeeds")

    def test_unloadable_animal_model_falls_back_to_main_detector(self):
        primary = StubDetector([Detection("dog", 0.8, (0.2, 0.2, 0.2, 0.2))])
        calls = []

        def fake_onnx(settings, threshold):
            calls.append((settings, threshold))
            if settings.get("model") == "mdv6_apa_rtdetr_c":
                raise FileNotFoundError("model.onnx missing: export it")
            return primary

        with mock.patch("sentinel.detectors_onnx.OnnxDetector", side_effect=fake_onnx), self.assertLogs("sentinel.detectors", "WARNING"):
            detector = create_detector({"type": "onnx", "model": "yolox_s", "providers": "cpu", "animal_detector": {}}, 0.5)
        self.assertIsInstance(detector, AnimalAugmentedDetector)
        self.assertIsNone(detector.animal)
        self.assertEqual(detector.animal_status[0], "degraded")
        self.assertEqual([d.label for d in detector.detect(frame())], ["dog"])
        animal_settings, animal_threshold = calls[1]
        self.assertEqual((animal_settings["classes"], animal_settings["providers"]), (["animal"], "cpu"), "inherits the camera's providers")
        self.assertEqual(animal_threshold, 0.6)

    def test_switched_off_animal_category_skips_the_animal_detector(self):
        settings = with_category_classes({"type": "onnx", "model": "yolox_s", "animal_detector": {}},
                                         effective_categories({"categories": {"animal": {"detect": False}}}))
        primary = StubDetector()
        with mock.patch("sentinel.detectors_onnx.OnnxDetector", return_value=primary) as onnx:
            detector = create_detector(settings, 0.5)
        self.assertIs(detector, primary)
        self.assertEqual(onnx.call_count, 1)

    def test_disabled_add_on_is_ignored(self):
        primary = StubDetector()
        with mock.patch("sentinel.detectors_onnx.OnnxDetector", return_value=primary):
            self.assertIs(create_detector({"type": "onnx", "model": "yolox_s", "animal_detector": {"enabled": False}}, 0.5), primary)


class ConfigTests(unittest.TestCase):
    def base(self, animal):
        return {"site": {"id": "s"}, "cameras": [{"id": "c1", "source": {"type": "simulated"}}],
                "detector": {"type": "onnx", "model": "auto", "animal_detector": animal}}

    def test_valid_block(self):
        validate_config(self.base({"model": "mdv6_apa_rtdetr_c", "score_threshold": 0.4}))
        validate_config(self.base({}))

    def test_invalid_blocks(self):
        for block, message in (([], "must be an object"), ({"score_threshold": 1.5}, "between 0 and 1"),
                               ({"threshold": 0.3}, "unknown keys"), ({"enabled": "yes"}, "true or false")):
            with self.subTest(block=block), self.assertRaises(ConfigError) as caught:
                validate_config(self.base(block))
            self.assertIn(message, str(caught.exception))

    def test_unknown_or_unreviewed_model_rejected(self):
        try:
            import sentinel.detectors_onnx  # noqa: F401
        except ImportError:
            self.skipTest("onnxruntime not installed")
        for model, message in (("mdv5a", "not a known ONNX model"), ("rfdetr_base", "licence review")):
            with self.subTest(model=model), self.assertRaises(ConfigError) as caught:
                validate_config(self.base({"model": model}))
            self.assertIn(message, str(caught.exception))


@unittest.skipIf(np is None, "numpy not installed")
class VideoWorkflowTests(unittest.TestCase):
    """scripts/guardian/video_eval.py on a generated clip: an 'animal' crosses for
    2 s, then the scene is empty for 1 s."""

    def test_scores_presence_false_positives_and_guardian_tracks(self):
        import importlib.util
        import tempfile

        import cv2

        spec = importlib.util.spec_from_file_location("video_eval", ROOT / "scripts/guardian/video_eval.py")
        video_eval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(video_eval)

        class BlobDetector:
            def detect(self, frame):
                ys, xs = np.nonzero(frame.image[..., 0] > 128)
                if not len(xs):
                    return []
                h, w = frame.image.shape[:2]
                return [Detection("animal", 0.9, (xs.min() / w, ys.min() / h, (xs.max() - xs.min()) / w, (ys.max() - ys.min()) / h))]

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 240))
            for index in range(30):
                image = np.zeros((240, 320, 3), np.uint8)
                if index < 20:
                    x = 20 + index * 10
                    image[100:160, x:x + 60] = 255
                writer.write(image)
            writer.release()
            clip = {"id": "blob", "light": "day", "animal": "test blob", "expected": None,
                    "present_s": [[0, 1.9]], "absent_s": [[2.2, 3]]}
            with mock.patch.dict(video_eval.DETECTORS, {"blob": {"type": "scripted"}}), \
                    mock.patch("sentinel.detectors.create_detector", return_value=BlobDetector()):
                result = video_eval.run_clip(clip, path, "blob", 5.0, False, Path(tmp) / "storage")
        self.assertEqual(result["presence_recall"], 1.0)
        self.assertEqual(result["false_positive_frames"], "0/4")
        self.assertEqual(result["guardian_tracks"], 1, "one animal crossing is one Guardian track")
        self.assertGreaterEqual(result["longest_track_s"], 1.0)
        self.assertEqual(result["species_confirmed_wrong"], [])
        self.assertIn("Totals", video_eval.render([result | {"expects_species": False}], False))


@unittest.skipUnless(MD_MODEL.exists() and np is not None, "MegaDetector ONNX export not present (scripts/guardian/export_megadetector_onnx.py)")
class RealModelTests(unittest.TestCase):
    def test_finds_the_black_bear_yolox_missed(self):
        import cv2

        manifest = ROOT / "data/guardian/runs/initial/manifest.jsonl"
        if not manifest.exists():
            self.skipTest("Guardian wildlife sample not downloaded")
        record = next((json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if '"ena24:9310"' in line), None)
        if record is None:
            self.skipTest("ena24:9310 not in the sample")
        image = cv2.imread(str(manifest.parent / record["path"]))
        frame = Frame(image, 0)
        yolox = create_detector({"type": "onnx", "model": "yolox_s", "input_size": [960, 544], "providers": "cpu", "classes": ["bear", "dog", "cat"]}, 0.5)
        self.assertFalse(yolox.detect(frame), "YOLOX misses this black bear (the gap MegaDetector closes)")
        detector = create_detector({"type": "onnx", "model": "mdv6_apa_rtdetr_c", "providers": "cpu", "classes": ["animal"]}, 0.6)
        animals = [d for d in detector.detect(frame) if d.label == "animal"]
        self.assertTrue(animals, "MegaDetector finds the bear")
        x, y, w, h = record["source_annotations"][0]["bbox"]
        truth = (x / record["width"], y / record["height"], w / record["width"], h / record["height"])
        from sentinel.tracker import iou

        self.assertGreater(max(iou(d.bbox, truth) for d in animals), 0.5)


if __name__ == "__main__":
    unittest.main()
