import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sentinel.bench.labels import GTBox, load_caviar, load_mot
from sentinel.bench.metrics import DetectionStats, ExpectedEvent, TrackingStats, match_boxes, match_events, percentiles
from sentinel.models import Detection

try:
    import cv2
    import numpy as np
except ImportError:  # video tests need OpenCV
    cv2 = None


def gt(track_id, bbox, ignore=False, visibility=1.0):
    return GTBox(track_id, "person", bbox, ignore=ignore, visibility=visibility)


class LabelTests(unittest.TestCase):
    def test_mot_classes_and_ignore_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gt.txt"
            path.write_text(
                "1,1,100,100,50,100,1,1,0.9\n"   # scored pedestrian
                "1,2,300,100,50,100,1,7,1.0\n"   # static person: real, not scored
                "1,3,500,100,50,100,0,1,0.1\n"   # pedestrian flagged not-considered
                "1,4,700,100,50,100,1,8,1.0\n"   # distractor: not a person
                "2,5,10,10,100,50,1,3,1.0\n"     # car: not person ground truth
            )
            labels = load_mot(path, 1000, 500, 25)
        boxes = {box.track_id: box for box in labels.at_frame(0)}
        self.assertEqual(set(boxes), {"mot1", "mot2", "mot3", "mot4"})
        self.assertFalse(boxes["mot1"].ignore)
        self.assertTrue(boxes["mot2"].ignore and boxes["mot2"].real_person)
        self.assertTrue(boxes["mot3"].ignore)
        self.assertFalse(boxes["mot4"].real_person)
        self.assertAlmostEqual(boxes["mot1"].bbox[0], 0.1)
        self.assertEqual(labels.at_frame(1), [])

    def test_labels_keep_their_own_resolution_and_rate(self):
        from sentinel.bench.labels import load_labels

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gt.txt"
            path.write_text("31,1,960,540,96,216,1,1,1.0\n")
            # video re-encoded to 640x360 @ 10 fps; annotations are 1920x1080 @ 30 fps
            labels = load_labels({"format": "mot", "path": str(path), "width": 1920, "height": 1080, "fps": 30}, 640, 360, 10)
        self.assertEqual(labels.fps, 30)
        box = labels.at_frame(30)[0]
        self.assertAlmostEqual(box.bbox[0], 0.5)
        self.assertAlmostEqual(box.bbox[3], 0.2)

    def test_caviar_boxes_behaviours_and_left_objects(self):
        xml = """<?xml version="1.0"?><dataset name="x">
        <frame number="0"><objectlist>
          <object id="0"><box h="60" w="20" xc="100" yc="100"/><appearance>visible</appearance>
            <hypothesislist><hypothesis><role>walker</role><situation>moving</situation></hypothesis></hypothesislist></object>
          <object id="1"><box h="4" w="20" xc="10" yc="280"/><appearance>appear</appearance>
            <hypothesislist><hypothesis><role>walker</role></hypothesis></hypothesislist></object>
          <object id="2"><box h="20" w="16" xc="200" yc="200"/><appearance>visible</appearance>
            <hypothesislist><hypothesis><role>leaving object</role></hypothesis></hypothesislist></object>
        </objectlist></frame>
        <frame number="1"><objectlist>
          <object id="0"><box h="60" w="20" xc="104" yc="100"/><appearance>visible</appearance>
            <hypothesislist><hypothesis><role>walker</role><situation>moving</situation></hypothesis></hypothesislist></object>
        </objectlist></frame></dataset>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gt.xml"
            path.write_text(xml)
            labels = load_caviar(path, 384, 288, 25)
        frame0 = {box.track_id: box for box in labels.at_frame(0)}
        self.assertFalse(frame0["cav0"].ignore)
        self.assertTrue(frame0["cav1"].ignore, "entry frames are don't-care")
        self.assertEqual(frame0["cav2"].label, "bag")
        self.assertEqual(labels.behaviours["cav0"], [(0, 1, "moving")])


class MetricTests(unittest.TestCase):
    def test_match_boxes_counts_and_ignores(self):
        truth = [gt("a", (0.1, 0.1, 0.1, 0.3)), gt("b", (0.5, 0.1, 0.1, 0.3)), gt("z", (0.8, 0.1, 0.1, 0.3), ignore=True)]
        predictions = [(0.11, 0.1, 0.1, 0.3), (0.81, 0.1, 0.1, 0.3), (0.3, 0.6, 0.1, 0.1)]
        matches, unmatched, missed = match_boxes(predictions, truth)
        self.assertEqual([(p, g.track_id) for p, g in matches], [(0, "a")])
        self.assertEqual(unmatched, [2], "prediction on a don't-care region is neither TP nor FP")
        self.assertEqual([g.track_id for g in missed], ["b"])
        stats = DetectionStats()
        stats.add(matches, unmatched, missed)
        summary = stats.summary()
        self.assertEqual((summary["precision"], summary["recall"]), (0.5, 0.5))
        self.assertEqual(summary["recall_by_size"]["large"], 0.5)

    def test_visible_recall(self):
        stats = DetectionStats()
        stats.add([(0, gt("a", (0, 0, 0.1, 0.3)))], [], [gt("b", (0.5, 0, 0.1, 0.3), visibility=0.1)])
        summary = stats.summary()
        self.assertEqual(summary["recall"], 0.5)
        self.assertEqual(summary["recall_visible"], 1.0)

    def test_tracking_switches_and_coverage(self):
        stats = TrackingStats()
        person = gt("a", (0, 0, 0.1, 0.3))
        for frame in range(10):
            track = "t1" if frame < 6 else "t2"
            stats.add([(track, person)], [person])
        summary = stats.summary()
        self.assertEqual(summary["id_switches"], 1)
        self.assertEqual(summary["mostly_tracked"], 1)
        self.assertEqual(summary["mean_tracks_per_identity"], 2)

    def test_event_matching_is_one_to_one_within_window(self):
        start = 1_000_000
        predicted = [
            {"event_type": "zone_entry", "camera_id": "c", "zone_id": "z", "timestamp_ms": start + 10_500},
            {"event_type": "zone_entry", "camera_id": "c", "zone_id": "z", "timestamp_ms": start + 11_000},
            {"event_type": "dwell", "camera_id": "c", "zone_id": "z", "timestamp_ms": start + 50_000},
        ]
        expected = [
            ExpectedEvent("zone_entry", 10.0, "c", "z"),
            ExpectedEvent("dwell", 30.0, "c", "z", window_s=(-5, 10)),
            ExpectedEvent("zone_entry", 40.0, "c", "z"),
        ]
        result = match_events(predicted, expected, start)
        entry = result["per_type"]["zone_entry"]
        self.assertEqual((entry["matched"], entry["missed"], entry["false"]), (1, 1, 1))
        self.assertEqual(result["per_type"]["dwell"]["matched"], 0, "20 s late is outside the window")
        self.assertEqual(entry["latency_s"]["p50"], 0.5)

    def test_percentiles(self):
        self.assertEqual(percentiles([]), {"n": 0, "p50": None, "p95": None, "mean": None, "max": None})
        self.assertEqual(percentiles([1, 2, 3, 4])["max"], 4)


class FakeDetector:
    """Returns the scripted person box with a little noise and periodic misses."""

    def __init__(self, positions):
        self.positions = positions
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        box = self.positions.get(frame.timestamp_ms)
        if box is None or self.calls % 7 == 0:
            return []
        return [Detection("person", 0.9, box)]

    def close(self):
        pass


@unittest.skipIf(cv2 is None, "OpenCV not installed")
class RunnerTests(unittest.TestCase):
    """End to end on a tiny synthetic video, so the runner, report and viewer
    are exercised without downloading datasets."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.root = root
        fps, frames = 10, 120
        writer = cv2.VideoWriter(str(root / "clip.avi"), cv2.VideoWriter_fourcc(*"MJPG"), fps, (320, 240))
        self.boxes = {}
        rows = []
        for index in range(frames):
            image = np.full((240, 320, 3), 40, np.uint8)
            x = min(0.8, 0.05 + index * 0.01)
            box = (x, 0.4, 0.08, 0.4)
            cv2.rectangle(image, (int(x * 320), 96), (int((x + 0.08) * 320), 192), (220, 220, 220), -1)
            writer.write(image)
            self.boxes[index] = box
            rows.append({"frame": index, "id": "p1", "bbox": list(box)})
        writer.release()
        (root / "labels.json").write_text(json.dumps({"boxes": rows, "notes": "synthetic"}))
        site = {
            "site": {"id": "t", "map": {"width_m": 1, "height_m": 1, "origin_geo": [45, -75]}},
            "cameras": [{
                "id": "cam", "fps": 5, "source": {"type": "file", "path": str(root / "clip.avi")},
                "zones": [
                    {"id": "yard", "name": "Yard", "type": "yard", "polygon": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]},
                    {"id": "shed", "name": "Shed", "type": "restricted", "polygon": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]},
                ],
            }],
            "detector": {"type": "mediapipe"},
            "tracker": {"min_hits": 2},
            "rules": [{"id": "shed_entry", "type": "zone_entry", "zone_types": ["restricted"], "severity": "high"}],
            "alerts": {"channels": []},
        }
        (root / "site.json").write_text(json.dumps(site))
        manifest = {
            "site_config": str(root / "site.json"),
            "start_time": "2026-10-13T14:00:00",
            "clips": [{"id": "cam", "camera_id": "cam", "video": str(root / "clip.avi"),
                       "labels": {"format": "sentinel", "path": str(root / "labels.json")}, "conditions": ["synthetic_test"]}],
        }
        (root / "manifest.json").write_text(json.dumps(manifest))

    def tearDown(self):
        self.directory.cleanup()

    def test_run_report_and_viewer(self):
        from sentinel.bench import runner
        from sentinel.bench.report import aggregate, write_reports

        start_ms = int(__import__("datetime").datetime.fromisoformat("2026-10-13T14:00:00").timestamp() * 1000)
        positions = {start_ms + step * 200: self.boxes[min(119, step * 2)] for step in range(60)}
        manifest = runner.load_manifest(self.root / "manifest.json")
        with mock.patch.object(runner, "create_detector", return_value=FakeDetector(positions)):
            result = runner.run_offline(manifest, ["cam"], self.root / "run" / "s1", session_id="s1", motion_gate=False)
        clip = result["clips"][0]
        self.assertGreater(clip["frames_processed"], 50)
        detection = clip["detection"]
        self.assertEqual(detection["precision"], 1.0)
        self.assertLess(detection["recall"], 1.0, "the fake detector drops every 7th frame")
        self.assertGreater(detection["recall"], 0.8)
        self.assertEqual(clip["events"]["zone_entry"]["matched"], 1)
        self.assertEqual(clip["events"]["zone_entry"]["false"], 0)
        self.assertEqual(clip["notification_match"]["matched"], 1)

        report = aggregate([result])
        write_reports(report, self.root / "run")
        markdown = (self.root / "run" / "report.md").read_text()
        self.assertIn("Read this first", markdown)
        self.assertIn("synthetic_test", markdown)

        try:
            from fastapi.testclient import TestClient
        except ImportError:
            return
        from sentinel.bench.viewer import create_viewer

        with TestClient(create_viewer(self.root / "run")) as client:
            self._check_viewer(client)

    def _check_viewer(self, client):
        sessions = client.get("/api/sessions").json()
        self.assertEqual(sessions[0]["session"], "s1")
        timeline = client.get("/api/timeline", params={"session": "s1", "clip": "cam"}).json()
        self.assertTrue(timeline["has_gt"])
        self.assertTrue(any(event["type"] == "zone_entry" and event["matched"] for event in timeline["predicted"]))
        frame = client.get("/api/frame", params={"session": "s1", "clip": "cam", "i": timeline["frames"][5]["i"]})
        self.assertEqual(frame.headers["content-type"], "image/jpeg")
        self.assertEqual(client.get("/api/timeline", params={"session": "../..", "clip": "cam"}).status_code, 404)


@unittest.skipIf(cv2 is None, "OpenCV not installed")
class DegradeTests(unittest.TestCase):
    def test_degradations_keep_shape_and_are_deterministic(self):
        from sentinel.bench.degrade import DEGRADATIONS, apply

        image = np.full((60, 80, 3), 128, np.uint8)
        for kind in DEGRADATIONS:
            first = apply(image, kind, 3)
            self.assertEqual(first.shape, image.shape)
            self.assertTrue(np.array_equal(first, apply(image, kind, 3)))
        self.assertLess(apply(image, "low_light", 0).mean(), image.mean())


if __name__ == "__main__":
    unittest.main()
