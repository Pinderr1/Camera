"""Environmental motion analysis on synthetic sequences (no datasets needed).
These check the heuristics' direction, not real-world accuracy."""

import unittest

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None


def textured(seed=1):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur((rng.random((360, 640, 3)) * 255).astype(np.uint8), (15, 15), 0)


def run(frames, detections=None):
    from sentinel.environment import EnvironmentMonitor

    monitor = EnvironmentMonitor()
    seen, unknown = {}, 0
    for index, frame in enumerate(frames):
        report = monitor.update(frame, index * 200, detections(index) if detections else [])
        if index >= 35:  # after background warm-up
            for name, active in report.conditions.items():
                seen[name] = seen.get(name, 0) + int(active)
            unknown += len(report.unknown)
    return {name: count for name, count in seen.items() if count}, unknown


@unittest.skipIf(cv2 is None, "OpenCV not installed")
class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.base = textured()

    def test_static_scene_is_quiet(self):
        seen, unknown = run([self.base.copy() for _ in range(80)])
        self.assertEqual((seen, unknown), ({}, 0))

    def test_lighting_jump(self):
        bright = cv2.convertScaleAbs(self.base, alpha=1, beta=60)
        seen, unknown = run([self.base.copy() for _ in range(60)] + [bright for _ in range(20)])
        self.assertIn("lighting_change", seen)
        self.assertNotIn("near_camera_object", seen)
        self.assertEqual(unknown, 0)

    def test_rain_is_precipitation_not_an_object(self):
        from sentinel.bench.degrade import apply

        seen, unknown = run([self.base.copy() for _ in range(40)] + [apply(self.base, "rain", i) for i in range(40)])
        self.assertIn("precipitation_or_noise", seen)
        self.assertEqual(unknown, 0)

    def test_ir_switch(self):
        grey = cv2.cvtColor(cv2.cvtColor(self.base, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        seen, _ = run([grey for _ in range(60)])
        self.assertIn("ir_mode", seen)

    def mover(self, index):
        frame = self.base.copy()
        if index >= 40:
            x = 60 + (index - 40) * 8
            cv2.rectangle(frame, (x, 200), (x + 30, 250), (30, 30, 200), -1)
        return frame

    def test_unexplained_mover_becomes_unknown_motion(self):
        _, unknown = run([self.mover(i) for i in range(80)])
        self.assertGreater(unknown, 10)

    def test_mover_explained_by_a_detection_is_not_unknown(self):
        from sentinel.models import Detection

        def detections(index):
            return [Detection("person", 0.9, ((60 + (index - 40) * 8) / 640, 200 / 360, 30 / 640, 50 / 360))] if index >= 40 else []

        _, unknown = run([self.mover(i) for i in range(80)], detections)
        self.assertEqual(unknown, 0)

    def test_swaying_vegetation_is_recurring_motion_not_unknown(self):
        def sway(index):
            frame = self.base.copy()
            cv2.ellipse(frame, (500 + int(12 * np.sin(index / 2)), 100), (60, 40), 0, 0, 360, (40, 160, 40), -1)
            return frame

        seen, unknown = run([sway(i) for i in range(300)])
        self.assertIn("recurring_motion", seen)
        self.assertEqual(unknown, 0)


if __name__ == "__main__":
    unittest.main()
