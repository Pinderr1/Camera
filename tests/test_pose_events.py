import json
import math
import unittest

from senior_safety.pose_events import FrameFeatures, PoseEventEngine, point_in_polygon, torso_angle_deg

with open("config/zones.example.json", encoding="utf-8") as handle:
    ZONES_CONFIG = json.load(handle)


def frame(timestamp_ms, hip=None, shoulder_mid=None, jitter=0.0, present=None, full_body_confidence=0.9):
    if present is None:
        present = hip is not None
    keypoints = {}
    if hip is not None:
        offset = jitter if (timestamp_ms // 100) % 2 == 0 else -jitter
        keypoints = {"kp_a": (hip[0] + offset, hip[1]), "kp_b": (hip[0], hip[1] + offset)}
    shoulder = shoulder_mid or ((hip[0], hip[1] - 0.25) if hip else None)
    torso_length = math.hypot(shoulder[0] - hip[0], shoulder[1] - hip[1]) if hip and shoulder else None
    return FrameFeatures(
        timestamp_ms=timestamp_ms,
        person_present=present,
        hip=hip,
        shoulder_mid=shoulder,
        bbox=(hip[0] - 0.1, hip[1] - 0.3, 0.2, 0.5) if hip else None,
        keypoints=keypoints,
        confidence=0.9 if present else 0.0,
        full_body_confidence=full_body_confidence if present else 0.0,
        torso_length=torso_length,
    )


def fall_sequence(start_hip=(0.6, 0.40), fallen_hip=(0.6, 0.75), fallen_shoulder=(0.75, 0.74), until_ms=13000):
    frames = [frame(t, hip=start_hip, jitter=0.01) for t in range(0, 1100, 100)]
    frames.append(frame(1100, hip=(start_hip[0], 0.52)))
    frames.append(frame(1200, hip=(start_hip[0], 0.64)))
    frames.extend(frame(t, hip=fallen_hip, shoulder_mid=fallen_shoulder) for t in range(1300, until_ms, 100))
    return frames


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
        events = names_values(run_frames(engine, fall_sequence()))

        self.assertIn(("fall_suspected", True), events)
        self.assertIn(("floor_level_posture", True), events)
        self.assertIn(("no_motion", True), events)

    def test_partial_body_fall_emits_lower_confidence_fall_suspected(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [
            frame(f.timestamp_ms, hip=f.hip, shoulder_mid=f.shoulder_mid, full_body_confidence=0.35)
            for f in fall_sequence(until_ms=5000)
        ]
        events = run_frames(engine, frames)
        falls = [e for e in events if e["event_name"] == "fall_suspected" and e["value"]]

        self.assertEqual(len(falls), 1)
        self.assertLess(falls[0]["confidence"], 0.65)
        self.assertEqual(falls[0]["notes"], "partial_body")

    def test_hip_glitch_spike_does_not_trigger_fall(self):
        # Reproduces the 2026-07-06 live false positive: the hip landmark
        # teleports far above the frame for a frame or two, then snaps back.
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.6, 0.42), jitter=0.01) for t in range(0, 2000, 100)]
        frames.append(frame(2000, hip=(0.6, -0.92), shoulder_mid=(0.6, 0.17)))
        frames.append(frame(2100, hip=(0.6, -0.92), shoulder_mid=(0.6, 0.17)))
        frames.extend(frame(t, hip=(0.6, 0.42), jitter=0.01) for t in range(2200, 6000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertNotIn(("fall_suspected", True), events)
        self.assertNotIn(("floor_level_posture", True), events)

    def test_uncorroborated_drop_does_not_emit_fall(self):
        # Rapid descent that immediately recovers to standing: no fall event.
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.6, 0.40), jitter=0.01) for t in range(0, 1100, 100)]
        frames.append(frame(1100, hip=(0.6, 0.52)))
        frames.append(frame(1200, hip=(0.6, 0.64)))
        frames.extend(frame(t, hip=(0.6, 0.42), jitter=0.01) for t in range(1300, 6000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertNotIn(("fall_suspected", True), events)

    def test_slow_lie_down_does_not_emit_fall_suspected(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.6, 0.40), jitter=0.01) for t in range(0, 2000, 100)]
        # Descend 0.35 units over 4 seconds: controlled, not a fall.
        for i, t in enumerate(range(2000, 6000, 100)):
            frames.append(frame(t, hip=(0.6, 0.40 + 0.35 * i / 39)))
        frames.extend(frame(t, hip=(0.6, 0.75), shoulder_mid=(0.75, 0.74)) for t in range(6000, 12000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertNotIn(("fall_suspected", True), events)
        self.assertIn(("floor_level_posture", True), events)

    def test_fall_evidence_survives_brief_dropout(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = fall_sequence(until_ms=8000)
        frames.extend(frame(t, present=False) for t in range(8000, 13000, 100))
        frames.extend(frame(t, hip=(0.6, 0.75), shoulder_mid=(0.75, 0.74)) for t in range(13000, 16000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertIn(("floor_level_posture", True), events)
        self.assertNotIn(("floor_level_posture", False), events)
        self.assertEqual(events.count(("floor_level_posture", True)), 1)

    def test_long_absence_wipes_evidence_silently(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = fall_sequence(until_ms=8000)
        frames.extend(frame(t, present=False) for t in range(8000, 50000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertIn(("floor_level_posture", True), events)
        self.assertNotIn(("floor_level_posture", False), events)
        self.assertNotIn(("no_motion", False), events)

    def test_floor_latch_ignores_single_frame_flicker(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = fall_sequence(until_ms=1300)
        for t in range(1300, 13000, 100):
            if (t // 100) % 10 == 0:
                # One upright-shoulder frame in ten: pose flicker, not recovery.
                frames.append(frame(t, hip=(0.6, 0.75), shoulder_mid=(0.6, 0.5)))
            else:
                frames.append(frame(t, hip=(0.6, 0.75), shoulder_mid=(0.75, 0.74)))
        events = names_values(run_frames(engine, frames))

        self.assertIn(("floor_level_posture", True), events)
        self.assertNotIn(("floor_level_posture", False), events)

    def test_zone_boundary_flicker_does_not_toggle_bed(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = []
        for t in range(0, 10000, 100):
            x = 0.41 if (t // 100) % 2 == 0 else 0.43
            frames.append(frame(t, hip=(x, 0.6), jitter=0.01))
        events = names_values(run_frames(engine, frames))

        self.assertNotIn(("bed_occupied", True), events)
        self.assertNotIn(("bed_occupied", False), events)

    def test_bed_zone_suppresses_fall_signals(self):
        engine = PoseEventEngine(ZONES_CONFIG)
        frames = [frame(t, hip=(0.2, 0.40), jitter=0.01) for t in range(0, 1100, 100)]
        frames.append(frame(1100, hip=(0.2, 0.55)))
        frames.extend(frame(t, hip=(0.2, 0.75), shoulder_mid=(0.35, 0.74)) for t in range(1200, 5000, 100))
        events = names_values(run_frames(engine, frames))

        self.assertNotIn(("fall_suspected", True), events)
        self.assertNotIn(("floor_level_posture", True), events)

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
