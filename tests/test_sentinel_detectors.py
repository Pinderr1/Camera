import json
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    import numpy as np
except ImportError:
    np = None

from sentinel.detectors import detection_thresholds
from sentinel.models import Detection
from sentinel.tracker import IoUTracker, TrackerConfig
from sentinel.vision import Frame


class FakeSession:
    def __init__(self, input_shape, outputs):
        self._input_shape = input_shape
        self._outputs = outputs  # name -> array
        self.fed = None

    def get_inputs(self):
        return [SimpleNamespace(name="input", shape=self._input_shape)]

    def get_outputs(self):
        return [SimpleNamespace(name=name) for name in self._outputs]

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, _names, feed):
        self.fed = feed["input"]
        return list(self._outputs.values())


def onnx_detector(settings, session, threshold=0.4):
    from sentinel import detectors_onnx

    with mock.patch.object(detectors_onnx, "_session", return_value=session), mock.patch.object(
        detectors_onnx, "resolve_providers", return_value=["DmlExecutionProvider", "CPUExecutionProvider"]
    ), mock.patch("pathlib.Path.exists", return_value=True):
        return detectors_onnx.OnnxDetector(settings, threshold)


@unittest.skipIf(np is None, "numpy not installed")
class OnnxDecodeTests(unittest.TestCase):
    def test_yolox_grid_decode_and_letterbox_mapping(self):
        out = np.zeros((1, 3549, 85), np.float32)  # 416x416: 52*52 + 26*26 + 13*13 cells
        cell = 10 * 52 + 20  # stride-8 grid, column 20 row 10
        out[0, cell, :4] = [0.5, 0.5, np.log(4.0), np.log(8.0)]  # centre (164, 84), size 32x64 px
        out[0, cell, 4] = 0.9  # objectness
        out[0, cell, 5] = 0.9  # person
        out[0, 0, 4], out[0, 0, 5 + 2] = 0.9, 0.9  # a car, filtered by the class allowlist below
        detector = onnx_detector({"model": "yolox_tiny", "classes": ["person"]}, FakeSession([1, 3, 416, 416], {"output": out}))
        image = np.zeros((208, 416, 3), np.uint8)  # 2:1 frame -> letterbox ratio 1.0, padded below
        detections = detector.detect(Frame(image, 0))
        self.assertEqual([d.label for d in detections], ["person"])
        x, y, w, h = detections[0].bbox
        self.assertAlmostEqual(x, (164 - 16) / 416, places=4)
        self.assertAlmostEqual(y, (84 - 32) / 208, places=4)
        self.assertAlmostEqual(w, 32 / 416, places=4)
        self.assertAlmostEqual(h, 64 / 208, places=4)
        self.assertAlmostEqual(detections[0].confidence, 0.81, places=4)
        json.dumps([detections[0].confidence, *detections[0].bbox])  # numpy scalars would break event logs

    def test_detr_sigmoid_threshold_and_dynamic_aspect_input(self):
        logits = np.full((1, 300, 80), -10.0, np.float32)
        logits[0, 0, 0] = 2.0  # person, sigmoid ~0.88
        logits[0, 1, 0] = -1.0  # person below threshold
        boxes = np.zeros((1, 300, 4), np.float32)
        boxes[0, 0] = [0.5, 0.5, 0.1, 0.4]
        session = FakeSession(["batch", 3, "height", "width"], {"logits": logits, "pred_boxes": boxes})
        detector = onnx_detector({"model": "rtdetrv2_r18", "input_size": [960, 544], "keep_aspect": True}, session)
        detections = detector.detect(Frame(np.zeros((1080, 1920, 3), np.uint8), 0))
        self.assertEqual(len(detections), 1)
        self.assertAlmostEqual(detections[0].bbox[0], 0.45, places=4)
        self.assertEqual(session.fed.shape, (1, 3, 544, 960), "16:9 input keeps aspect at multiples of 32")
        self.assertLessEqual(float(session.fed.max()), 1.0)

    def test_coco91_labels_for_rfdetr(self):
        logits = np.full((1, 300, 91), -10.0, np.float32)
        logits[0, 0, 1] = 3.0  # category id 1 = person
        logits[0, 1, 3] = 3.0  # category id 3 = car
        boxes = np.tile(np.array([0.5, 0.5, 0.2, 0.2], np.float32), (1, 300, 1))
        detector = onnx_detector({"model": "rfdetr_base"}, FakeSession(["b", 3, "h", "w"], {"logits": logits, "pred_boxes": boxes}))
        labels = sorted(d.label for d in detector.detect(Frame(np.zeros((100, 100, 3), np.uint8), 0)))
        self.assertEqual(labels, ["car", "person"])

    def test_dfine_is_kept_off_directml(self):
        logits = np.full((1, 300, 80), -10.0, np.float32)
        detector = onnx_detector({"model": "dfine_s"}, FakeSession(["b", 3, "h", "w"], {"logits": logits, "pred_boxes": np.zeros((1, 300, 4), np.float32)}))
        self.assertNotIn("DmlExecutionProvider", detector.providers)

    def test_registry_records_licence_and_training_data(self):
        from sentinel.detectors_onnx import MODELS

        for name, spec in MODELS.items():
            self.assertIn("license", spec, name)
            self.assertIn("training_data", spec, name)
            self.assertIn("commercial_ok", spec, name)
        self.assertFalse(MODELS["rfdetr_base"]["commercial_ok"], "Objects365 pre-training needs legal review")
        self.assertTrue(MODELS["yolox_s"]["commercial_ok"])


class ProfileTests(unittest.TestCase):
    def test_auto_picks_gpu_or_cpu_profile(self):
        from sentinel import detectors_onnx
        from sentinel.detectors import resolve_detector_settings

        with mock.patch.object(detectors_onnx, "gpu_available", return_value=True):
            gpu = resolve_detector_settings({"type": "onnx", "model": "auto", "classes": ["person"]})
        with mock.patch.object(detectors_onnx, "gpu_available", return_value=False):
            cpu = resolve_detector_settings({"type": "onnx", "model": "auto"})
        self.assertEqual((gpu["model"], gpu["input_size"], gpu["classes"]), ("yolox_s", [960, 544], ["person"]))
        self.assertEqual((cpu["model"], cpu["input_size"]), ("yolox_tiny", [416, 416]))
        explicit = resolve_detector_settings({"type": "onnx", "model": "rtdetrv2_r18"})
        self.assertEqual(explicit["model"], "rtdetrv2_r18")

    def test_falls_back_to_efficientdet_without_onnxruntime(self):
        import sys

        from sentinel.detectors import resolve_detector_settings

        with mock.patch.dict(sys.modules, {"sentinel.detectors_onnx": None}):
            fallback = resolve_detector_settings({"type": "onnx", "model": "auto", "input_size": [960, 544]})
        self.assertEqual((fallback["type"], fallback["model"]), ("mediapipe", "efficientdet_lite0"))
        self.assertNotIn("input_size", fallback)


class ThresholdTests(unittest.TestCase):
    def test_detection_thresholds(self):
        self.assertEqual(detection_thresholds({"score_threshold": 0.5}), (0.5, 0.0))
        self.assertEqual(detection_thresholds({"score_threshold": 0.5, "low_score_threshold": 0.2}), (0.2, 0.5))
        self.assertEqual(detection_thresholds({"score_threshold": 0.5, "low_score_threshold": 0.2}, -0.1), (0.2, 0.4))


class LowScoreTrackingTests(unittest.TestCase):
    def det(self, x, confidence):
        return Detection("person", confidence, (x, 0.3, 0.06, 0.3))

    def test_low_score_boxes_extend_tracks_but_never_start_them(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=2, high_score=0.5, max_missed_ms=500))
        tracker.update([self.det(0.20, 0.8)], 0)
        active, _ = tracker.update([self.det(0.21, 0.8)], 200)
        track_id = active[0].track_id
        # partially occluded for a second: only weak boxes
        for step in range(5):
            active, ended = tracker.update([self.det(0.22 + step * 0.01, 0.3), self.det(0.8, 0.3)], 400 + step * 200)
            self.assertEqual([t.track_id for t in active], [track_id])
            self.assertEqual(ended, [])
        self.assertEqual(len(tracker.tracks), 1, "a weak box on its own never starts a track")

    def test_without_split_weak_boxes_behave_as_before(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=1))
        tracker.update([self.det(0.8, 0.3)], 0)
        self.assertEqual(len(tracker.tracks), 1)


if __name__ == "__main__":
    unittest.main()
