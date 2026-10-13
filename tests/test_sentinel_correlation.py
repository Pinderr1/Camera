import unittest

from sentinel.correlation import CorrelationConfig, CrossCameraCorrelator, appearance_similarity
from sentinel.models import Track

LINK = {"from": "cam_a", "to": "cam_b", "min_transit_s": 2, "max_transit_s": 20}


def track(camera_id, number, t_ms, x=0.5, y=0.8, label="person", appearance=None):
    return Track(
        track_id=f"{camera_id}:{number}",
        camera_id=camera_id,
        label=label,
        bbox=(x - 0.03, y - 0.2, 0.06, 0.2),
        confidence=0.9,
        first_seen_ms=t_ms,
        last_seen_ms=t_ms,
        confirmed=True,
        appearance=appearance,
    )


def leave(correlator, t, now_ms, zones=None):
    correlator.assign(t, now_ms, zones or [])
    correlator.end(t, now_ms)


class CorrelationTests(unittest.TestCase):
    def test_links_within_transit_window(self):
        correlator = CrossCameraCorrelator([LINK])
        first = track("cam_a", 1, 0)
        leave(correlator, first, 1000)
        site = correlator.assign(track("cam_b", 1, 8000), 8000, [])
        self.assertEqual(site.members, ["cam_a:1", "cam_b:1"])
        self.assertEqual(site.links[-1]["kind"], "camera_link")
        self.assertLess(site.link_confidence, 1.0)

    def test_links_are_bidirectional_by_default(self):
        correlator = CrossCameraCorrelator([LINK])
        leave(correlator, track("cam_b", 1, 0), 1000)
        site = correlator.assign(track("cam_a", 1, 6000), 6000, [])
        self.assertEqual(len(site.members), 2)

    def test_no_link_outside_window_or_wrong_class(self):
        correlator = CrossCameraCorrelator([LINK])
        leave(correlator, track("cam_a", 1, 0), 1000)
        late = correlator.assign(track("cam_b", 1, 40_000), 40_000, [])
        self.assertEqual(late.members, ["cam_b:1"])
        correlator = CrossCameraCorrelator([LINK])
        leave(correlator, track("cam_a", 1, 0), 1000)
        vehicle = correlator.assign(track("cam_b", 2, 8000, label="vehicle"), 8000, [])
        self.assertEqual(vehicle.members, ["cam_b:2"])

    def test_no_link_while_subject_still_visible_elsewhere(self):
        correlator = CrossCameraCorrelator([LINK])
        correlator.assign(track("cam_a", 1, 0), 5000, [])  # still active on cam_a
        other = correlator.assign(track("cam_b", 1, 8000), 8000, [])
        self.assertEqual(other.members, ["cam_b:1"])

    def test_zone_constraints(self):
        link = dict(LINK, from_zones=["gate"], to_zones=["porch"])
        correlator = CrossCameraCorrelator([link])
        leave(correlator, track("cam_a", 1, 0), 1000, zones=["yard"])
        self.assertEqual(correlator.assign(track("cam_b", 1, 8000), 8000, ["porch"]).members, ["cam_b:1"])
        correlator = CrossCameraCorrelator([link])
        leave(correlator, track("cam_a", 1, 0), 1000, zones=["gate"])
        self.assertEqual(len(correlator.assign(track("cam_b", 1, 8000), 8000, ["porch"]).members), 2)

    def test_ambiguous_candidates_are_not_merged(self):
        correlator = CrossCameraCorrelator([LINK])
        leave(correlator, track("cam_a", 1, 0, x=0.3), 1000)
        leave(correlator, track("cam_a", 2, 0, x=0.7), 1200)
        arrival = correlator.assign(track("cam_b", 1, 9000), 9000, [])
        self.assertEqual(arrival.members, ["cam_b:1"])
        self.assertEqual(len(arrival.ambiguous_with), 2)
        self.assertEqual(correlator.ambiguous_count, 1)

    def test_appearance_breaks_the_tie(self):
        red, blue = [1.0, 0.0, 0.0, 0.1], [0.0, 0.1, 1.0, 0.0]
        correlator = CrossCameraCorrelator([LINK])
        leave(correlator, track("cam_a", 1, 0, x=0.3, appearance=red), 1000)
        leave(correlator, track("cam_a", 2, 0, x=0.7, appearance=blue), 1200)
        arrival = correlator.assign(track("cam_b", 1, 9000, appearance=[0.95, 0.05, 0.0, 0.1]), 9000, [])
        self.assertEqual(arrival.members, ["cam_a:1", "cam_b:1"])

    def test_same_camera_reacquisition(self):
        correlator = CrossCameraCorrelator([], CorrelationConfig(reacquire_s=4))
        leave(correlator, track("cam_a", 1, 0, x=0.40), 1000)
        site = correlator.assign(track("cam_a", 2, 3000, x=0.42), 3000, [])
        self.assertEqual(site.links[-1]["kind"], "reacquired")
        far = CrossCameraCorrelator([], CorrelationConfig(reacquire_s=4))
        leave(far, track("cam_a", 1, 0, x=0.1), 1000)
        self.assertEqual(far.assign(track("cam_a", 2, 3000, x=0.9), 3000, []).members, ["cam_a:2"])

    def test_expiry(self):
        correlator = CrossCameraCorrelator([], CorrelationConfig(retain_s=10))
        leave(correlator, track("cam_a", 1, 0), 1000)
        self.assertEqual(len(correlator.expire(20_000)), 1)
        self.assertEqual(correlator.site_tracks, {})

    def test_appearance_similarity(self):
        self.assertIsNone(appearance_similarity(None, [1.0]))
        self.assertAlmostEqual(appearance_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)


if __name__ == "__main__":
    unittest.main()
