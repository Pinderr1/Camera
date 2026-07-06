import json
import unittest

from senior_safety.pose_events import FrameFeatures, PoseEventEngine, point_in_polygon, torso_angle_deg

with open("config/zones.example.json", encoding="utf-8") as handle:
    ZONES_CONFIG = json.load(handle)


def frame(timestamp_ms, hip=None, shoulder_mid=None, jitter=0.0, present=None):
    if present is None:
        present = hip is not None
    keypoints = []
    if hip is not None:
        offset = jitter if (timestamp_ms // 100) % 2 == 0 else -jitter
        keypoints = [(hip[0] + offset, hip[1]), (hip[0], hip[1] + offset)]
    return FrameFeatures(
        timestamp_ms=timestamp_ms,
        person_present=present,
        hip=hip,
        shoulder_mid=shoulder_mid or ((hip[0], hip[1] - 0.25) if hip else None),
        bbox=(hip[0] - 0.1, hip[1] - 0.3, 0.2, 0.5) if hip else None,
        keypoints=keypoints,
        confidence=0.9 if present else 0.0,
    )


def run_frames(engine, frames):
    events = []
    for f in frames:
        events.extend(engine.update(f))
    return events


def names_values(events):
    return [(e["event_name"], e["value"]) for e in events]


class PoseEventEngineTests(unittest.TestCase):
    def test_point_in_polygon(self):
        square = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        self.assertTrue(point_in_polygon(0.5, 0.5, square))
        self.assertFalse(point_in_polygon(1.5, 0.5, square))

    def test_torso_angle(self):
        self.assertAlmostEqual(torso_angle_deg((0.5, 0.3), (0.5, 0.6)), 0.0)
        self.assertAlmostEqual(torso_angle_deg((0.8, 0.6), (0.5, 0.6)), 90.0)

    def test_walking_on_route_emits_presence_and_route_motion(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.6, 0.5), jitter=0.01) for t in range(0, 2000, 100)]
        events = names_values(run_frames(engine, frames))

        self.assertIn(("person_present", True), events)
        self.assertIn(("route_motion", True), events)
        self.assertNotIn(("fall_suspected", True), events)

    def test_rapid_drop_then_floor_then_stillness(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.6, 0.40), jitter=0.01) for t in range(0, 1100, 100)]
        frames.append(frame(1100, hip=(0.6, 0.52)))
        frames.append(frame(1200, hip=(0.6, 0.64)))
        frames.extend(
            frame(t, hip=(0.6, 0.75), shoulder_mid=(0.75, 0.74)) for t in range(1300, 13000, 100)
        )
        events = names_values(run_frames(engine, frames))

        self.assertIn(("fall_suspected", True), events)
        self.assertIn(("floor_level_posture", True), events)
        self.assertIn(("no_motion", True), events)

    def test_bathroom_occupancy_inferred_from_doorway_exit(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.9, 0.5), jitter=0.01) for t in range(0, 2000, 100)]
        frames.extend(frame(t, present=False) for t in range(2000, 12000, 100))
        frames.extend(frame(t, hip=(0.9, 0.5), jitter=0.01) for t in range(12000, 14000, 100))
        events = run_frames(engine, frames)
        pairs = names_values(events)

        self.assertIn(("person_present", False), pairs)
        self.assertIn(("bathroom_occupied", True), pairs)
        inferred = next(e for e in events if e["event_name"] == "bathroom_occupied" and e["value"])
        self.assertLess(inferred["confidence"], 0.9)
        self.assertEqual(pairs[-1], ("bathroom_occupied", False))

    def test_bed_occupancy_proxy_with_debounce(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.2, 0.6), jitter=0.01) for t in range(0, 8000, 100)]
        frames.extend(frame(t, hip=(0.6, 0.5), jitter=0.01) for t in range(8000, 16000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertIn(("bed_occupied", True), events)
        self.assertIn(("bed_occupied", False), events)
        self.assertLess(events.index(("bed_occupied", True)), events.index(("bed_occupied", False)))

    def test_lost_pose_does_not_clear_bed_occupancy(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.2, 0.6), jitter=0.01) for t in range(0, 8000, 100)]
        frames.extend(frame(t, present=False) for t in range(8000, 14000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertIn(("bed_occupied", True), events)
        self.assertNotIn(("bed_occupied", False), events)
        self.assertNotIn(("bathroom_occupied", True), events)


if __name__ == "__main__":
    unittest.main()
