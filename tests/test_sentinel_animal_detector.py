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


class ScheduleTests(unittest.TestCase):
    """MegaDetector runs on a schedule: a slow scan while no animal is around, every
    frame for a while after either detector saw one."""

    def run_frames(self, schedule, script):
        from sentinel.detectors import AnimalSchedule

        class Counting(StubDetector):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def detect(self, frame):
                self.calls += 1
                return list(script.get(frame.timestamp_ms, {}).get("animal", []))

        class Main(StubDetector):
            def detect(self, frame):
                return list(script.get(frame.timestamp_ms, {}).get("main", []))

        animal = Counting()
        detector = AnimalAugmentedDetector(Main(), animal, schedule=AnimalSchedule(**schedule))
        ran = []
        for t in range(0, 10_000, 200):  # 5 fps for 10 s
            before = animal.calls
            detector.detect(Frame(None, t))
            if animal.calls > before:
                ran.append(t)
        return ran, detector

    def test_scans_once_a_second_when_nothing_is_there(self):
        ran, detector = self.run_frames({"scan_interval_s": 1.0}, {})
        self.assertEqual(ran, list(range(0, 10_000, 1000)))
        self.assertEqual(detector.describe()["animal_detector"]["runs"], 10)

    def test_follows_every_frame_after_an_animal_then_falls_back(self):
        bear = Detection("animal", 0.8, (0.2, 0.2, 0.2, 0.2))
        ran, _ = self.run_frames({"scan_interval_s": 1.0, "follow_s": 3.0}, {2000: {"animal": [bear]}})
        self.assertEqual([t for t in ran if 2000 <= t <= 5000], list(range(2000, 5200, 200)), "every frame for 3 s")
        self.assertEqual([t for t in ran if t > 5000], [6000, 7000, 8000, 9000], "back to the 1 s scan")

    def test_main_detector_animal_also_starts_following(self):
        dog = Detection("dog", 0.8, (0.2, 0.2, 0.2, 0.2))
        ran, _ = self.run_frames({"scan_interval_s": 1.0, "follow_s": 1.0}, {1400: {"main": [dog]}})
        self.assertIn(1400, ran, "an animal seen by YOLOX brings MegaDetector in on the same frame")

    def test_set_aside_boxes_do_not_keep_it_following(self):
        from sentinel.detectors import AnimalSchedule

        water = Detection("animal", 0.7, (0.4, 0.6, 0.2, 0.1))
        animal = StubDetector([water])
        detector = AnimalAugmentedDetector(StubDetector(), animal, schedule=AnimalSchedule(scan_interval_s=1.0, follow_s=3.0))
        runs = []
        for t in range(0, 6000, 200):
            # From 1 s on, Guardian has set the water track aside; the camera worker passes its box.
            meta = {"set_aside_boxes": [(0.41, 0.6, 0.2, 0.1)]} if t >= 1000 else {}
            before = detector.runs
            detector.detect(Frame(None, t, meta=meta))
            if detector.runs > before:
                runs.append(t)
        self.assertEqual(runs[:5], [0, 200, 400, 600, 800], "following the new box until Guardian sets it aside")
        self.assertEqual([t for t in runs if t >= 4000], [4800, 5800], "3 s after the last un-set-aside box, back to the 1 s scan")
        self.assertTrue(detector._news([Detection("animal", 0.8, (0.0, 0.0, 0.1, 0.1))], [(0.41, 0.6, 0.2, 0.1)]),
                        "an animal elsewhere still counts")

    def test_half_rate_while_following(self):
        bear = Detection("animal", 0.8, (0.2, 0.2, 0.2, 0.2))
        script = {t: {"animal": [bear]} for t in range(0, 10_000, 200)}
        ran, _ = self.run_frames({"scan_interval_s": 1.0, "follow_interval_s": 0.4}, script)
        self.assertEqual(ran[:4], [0, 400, 800, 1200])

    def test_scan_zero_runs_every_frame(self):
        ran, _ = self.run_frames({"scan_interval_s": 0.0}, {})
        self.assertEqual(len(ran), 50)

    def test_paired_merge_gives_one_box_per_animal_with_the_specific_label(self):
        from sentinel.detectors import AnimalSchedule

        main = StubDetector([Detection("person", 0.9, (0.1, 0.1, 0.1, 0.3)), Detection("bear", 0.7, (0.5, 0.5, 0.2, 0.2)),
                             Detection("dog", 0.6, (0.8, 0.8, 0.1, 0.1))])
        animal = StubDetector([Detection("animal", 0.8, (0.48, 0.47, 0.26, 0.25)),  # same bear, different size (IoU 0.6)
                               Detection("animal", 0.7, (0.0, 0.7, 0.1, 0.1))])  # missed by YOLOX
        detections = AnimalAugmentedDetector(main, animal, schedule=AnimalSchedule()).detect(frame())
        self.assertEqual(sorted(d.label for d in detections), ["animal", "bear", "dog", "person"])
        bear = next(d for d in detections if d.label == "bear")
        self.assertEqual(bear.bbox, (0.48, 0.47, 0.26, 0.25), "MegaDetector's box, YOLOX's label")
        self.assertEqual(bear.confidence, 0.8)
        self.assertEqual(next(d for d in detections if d.label == "person").bbox, (0.1, 0.1, 0.1, 0.3), "people untouched")

    def test_two_animals_side_by_side_are_not_merged(self):
        from sentinel.detectors import AnimalSchedule

        main = StubDetector([Detection("cat", 0.7, (0.30, 0.5, 0.1, 0.2)), Detection("cat", 0.7, (0.42, 0.5, 0.1, 0.2))])
        animal = StubDetector([Detection("animal", 0.8, (0.30, 0.5, 0.1, 0.2)), Detection("animal", 0.8, (0.42, 0.5, 0.1, 0.2))])
        detections = AnimalAugmentedDetector(main, animal, schedule=AnimalSchedule()).detect(frame())
        self.assertEqual(len(detections), 2)
        self.assertEqual({d.label for d in detections}, {"cat"})


@unittest.skipIf(np is None, "numpy not installed")
class GpuLockTests(unittest.TestCase):
    def test_every_directml_session_shares_one_lock(self):
        # Measured: two different DirectML sessions running at once crash the process.
        try:
            from sentinel import detectors_onnx
        except ImportError:
            self.skipTest("onnxruntime not installed")

        class FakeOrtSession:
            def __init__(self, *args, **kwargs):
                pass

            def get_providers(self):
                return ["DmlExecutionProvider", "CPUExecutionProvider"]

        with mock.patch("onnxruntime.InferenceSession", FakeOrtSession), mock.patch.dict(detectors_onnx._SESSIONS, clear=True):
            a = detectors_onnx._session(Path("a.onnx"), ["DmlExecutionProvider"], 0)
            b = detectors_onnx._session(Path("b.onnx"), ["DmlExecutionProvider"], 0)
        self.assertIsNot(a, b)
        self.assertIs(a.lock, b.lock)

    def test_busy_gpu_raises_instead_of_waiting_forever(self):
        try:
            from sentinel import detectors_onnx
        except ImportError:
            self.skipTest("onnxruntime not installed")
        import threading

        lock = threading.Lock()
        shared = detectors_onnx._SharedSession(mock.Mock(), lock)
        lock.acquire()
        try:
            with mock.patch.object(detectors_onnx, "GPU_WAIT_S", 0.05), self.assertRaises(TimeoutError):
                shared.run(None, {})
        finally:
            lock.release()
        shared.run(None, {})  # free again


@unittest.skipIf(np is None, "numpy not installed")
class PrecisionTests(unittest.TestCase):
    """`detector.precision: fp16` (opt-in) uses a pre-converted copy on a GPU only."""

    def make(self, provider, exists, fail=False):
        try:
            from sentinel import detectors_onnx
            from test_sentinel_detectors import FakeSession
        except ImportError:
            self.skipTest("onnxruntime not installed")
        loaded = []

        class Session(FakeSession):
            def get_providers(self):
                return [provider, "CPUExecutionProvider"]

        def session(path, providers, threads):
            if fail and path.name.endswith(".fp16.onnx"):
                raise RuntimeError("DmlGraphFusionHelper: The parameter is incorrect")
            loaded.append(path.name)
            return Session([1, 3, "h", "w"], {"output": np.zeros((1, 10, 85), np.float32)})

        real_exists = Path.exists
        with mock.patch.object(detectors_onnx, "_session", side_effect=session), \
                mock.patch.object(detectors_onnx, "resolve_providers", return_value=[provider, "CPUExecutionProvider"]), \
                mock.patch("pathlib.Path.exists", lambda path: exists if path.name.endswith(".fp16.onnx") else True):
            detector = detectors_onnx.OnnxDetector({"model": "yolox_s", "model_path": "m/yolox_s.dynamic.onnx", "precision": "fp16"}, 0.5)
        return detector, loaded

    def test_gpu_with_copy_runs_fp16(self):
        detector, loaded = self.make("DmlExecutionProvider", exists=True)
        self.assertEqual(detector.describe()["precision"], "fp16")
        self.assertEqual(loaded[-1], "yolox_s.dynamic.fp16.onnx")

    def test_cpu_or_missing_copy_stays_fp32(self):
        with self.assertLogs("sentinel.detectors", "INFO"):
            detector, loaded = self.make("CPUExecutionProvider", exists=True)
        self.assertEqual((detector.precision, loaded), ("fp32", ["yolox_s.dynamic.onnx"]))
        with self.assertLogs("sentinel.detectors", "WARNING"):
            detector, _ = self.make("DmlExecutionProvider", exists=False)
        self.assertEqual(detector.precision, "fp32")

    def test_copy_that_will_not_load_stays_fp32(self):
        with self.assertLogs("sentinel.detectors", "WARNING") as logs:
            detector, _ = self.make("DmlExecutionProvider", exists=True, fail=True)
        self.assertEqual(detector.precision, "fp32")
        self.assertIn("does not load", "\n".join(logs.output))

    def test_config_accepts_only_fp32_or_fp16(self):
        base = {"site": {"id": "s"}, "cameras": [{"id": "c1", "source": {"type": "simulated"}}]}
        validate_config(base | {"detector": {"type": "onnx", "model": "auto", "precision": "fp16"}})
        with self.assertRaises(ConfigError):
            validate_config(base | {"detector": {"type": "onnx", "model": "auto", "precision": "int8"}})


class ConfigTests(unittest.TestCase):
    def base(self, animal):
        return {"site": {"id": "s"}, "cameras": [{"id": "c1", "source": {"type": "simulated"}}],
                "detector": {"type": "onnx", "model": "auto", "animal_detector": animal}}

    def test_valid_block(self):
        validate_config(self.base({"model": "mdv6_apa_rtdetr_c", "score_threshold": 0.4}))
        validate_config(self.base({"scan_interval_s": 0, "follow_interval_s": 0.4, "follow_s": 3, "merge": "add_missing"}))
        validate_config(self.base({}))

    def test_invalid_blocks(self):
        for block, message in (([], "must be an object"), ({"score_threshold": 1.5}, "between 0 and 1"),
                               ({"threshold": 0.3}, "unknown keys"), ({"enabled": "yes"}, "true or false"),
                               ({"scan_interval_s": -1}, "non-negative"), ({"merge": "union"}, "animal_box or add_missing")):
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
