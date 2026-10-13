import unittest

from sentinel.models import Detection
from sentinel.tracker import IoUTracker, TrackerConfig, iou


def det(x, y, w=0.1, h=0.2, label="person", confidence=0.9):
    return Detection(label, confidence, (x, y, w, h))


class TrackerTests(unittest.TestCase):
    def test_iou(self):
        self.assertAlmostEqual(iou((0, 0, 1, 1), (0, 0, 1, 1)), 1.0)
        self.assertEqual(iou((0, 0, 0.1, 0.1), (0.5, 0.5, 0.1, 0.1)), 0.0)

    def test_track_confirms_after_min_hits_and_keeps_id_while_moving(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=3))
        ids = []
        for step in range(6):
            active, _ = tracker.update([det(0.1 + step * 0.02, 0.5)], step * 200)
            ids.append([track.track_id for track in active])
        self.assertEqual(ids[:2], [[], []])
        self.assertEqual(len({tuple(item) for item in ids[2:]}), 1)

    def test_fast_mover_matched_by_centre_distance(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=1))
        first, _ = tracker.update([det(0.10, 0.5)], 0)
        second, _ = tracker.update([det(0.21, 0.5)], 200)  # no box overlap
        self.assertEqual(first[0].track_id, second[0].track_id)

    def test_labels_are_canonicalised_and_never_cross_matched(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=1))
        tracker.update([det(0.3, 0.5, label="car")], 0)
        active, _ = tracker.update([det(0.3, 0.5, label="person")], 200)
        self.assertEqual(len(tracker.tracks), 2)
        self.assertEqual({track.label for track in tracker.tracks.values()}, {"vehicle", "person"})
        self.assertEqual([track.label for track in active], ["person"])

    def test_track_ends_after_missed_timeout(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=1, max_missed_ms=1000))
        tracker.update([det(0.3, 0.5)], 0)
        _, ended = tracker.update([], 500)
        self.assertEqual(ended, [])
        _, ended = tracker.update([], 1600)
        self.assertEqual(len(ended), 1)

    def test_unconfirmed_tracks_end_silently(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=3, max_missed_ms=500))
        tracker.update([det(0.3, 0.5)], 0)
        _, ended = tracker.update([], 1000)
        self.assertEqual(ended, [])
        self.assertEqual(tracker.tracks, {})

    def test_displacement_tracks_arrival(self):
        tracker = IoUTracker("cam", TrackerConfig(min_hits=1))
        for step in range(5):
            tracker.update([det(0.1 + step * 0.05, 0.5)], step * 200)
        track = next(iter(tracker.tracks.values()))
        self.assertGreater(track.max_displacement, 0.15)


if __name__ == "__main__":
    unittest.main()
