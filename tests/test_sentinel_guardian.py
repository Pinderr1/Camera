"""Wilderness Guardian Phase 2: movement intelligence on synthetic track sequences
through the real tracker, plus runtime, notification, evidence and replay checks.
Synthetic sequences verify the logic only; they say nothing about accuracy on
real wildlife."""

from __future__ import annotations

import copy
import re
import tempfile
import unittest
from pathlib import Path

from sentinel.config import validate_config
from sentinel.guardian import GuardianMonitor
from sentinel.guardian.geometry import GroundCalibration
from sentinel.guardian.risk import FORBIDDEN_CLAIMS
from sentinel.incidents import DISCLAIMER
from sentinel.models import Detection
from sentinel.notify import CallbackNotifier
from sentinel.replay import run_scenario
from sentinel.runtime import SentinelRuntime
from sentinel.simulation import Scenario
from sentinel.tracker import IoUTracker
from senior_safety.config_validation import ConfigValidationError

from sentinel_helpers import NOON_MS, make_config

FPS = 5
ZONE = [[0.7, 0.55], [0.95, 0.55], [0.95, 0.95], [0.7, 0.95]]  # protected "Back door" on the right


def guardian_config(**guardian):
    config = make_config()
    config["cameras"][0]["zones"].append({"id": "back_door", "name": "Back door", "type": "entrance", "polygon": ZONE})
    config["guardian"] = {"enabled": True, "protected_zones": ["back_door"]} | guardian
    return config


def box(x, y, h, label="bear", aspect=1.3, confidence=0.85):
    """Detection with ground anchor (bottom-centre) at x, y and box height h."""
    w = h * aspect
    return Detection(label, confidence, (x - w / 2, y - h, w, h))


def drive(monitor, script, seconds, camera="cam_a", start=NOON_MS, tracker=None):
    """script(t) -> detections. Returns all guardian events."""
    tracker = tracker or IoUTracker(camera)
    events = []
    for step in range(int(seconds * FPS)):
        now = start + step * 1000 // FPS
        active, ended = tracker.update(script(step / FPS), now)
        events += monitor.process(camera, now, active, ended)
    return events


def levels(events):
    return [e.details["level"] for e in events]


class MovementClassificationTests(unittest.TestCase):
    def test_animal_approaching_the_camera(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        events = drive(monitor, lambda t: [box(0.4, 0.5 + 0.03 * t, 0.08 * (1.2 ** t))], 7)
        track = monitor.snapshot()[0]
        self.assertEqual(track["movement"]["movement"], "approaching")
        self.assertIn("the camera", track["targets"])
        self.assertIn("elevated_concern", levels(events))
        self.assertEqual(track["species"]["display"], "Bear (species not determined)")

    def test_animal_leaving_the_area(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        events = drive(monitor, lambda t: [box(0.4, 0.8 - 0.03 * t, 0.3 * (0.85 ** t))], 7)
        self.assertEqual(monitor.snapshot()[0]["movement"]["movement"], "retreating")
        self.assertEqual(events, [])

    def test_animal_moving_across_the_view(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[], protected_zone_types=[]))
        events = drive(monitor, lambda t: [box(0.1 + 0.08 * t, 0.5, 0.12)], 8)
        track = monitor.snapshot()[0]
        self.assertEqual(track["movement"]["movement"], "lateral")
        self.assertEqual(track["movement"]["image_heading"], "right")
        self.assertGreater(track["movement"]["relative_speed"], 0.3)
        self.assertEqual(events, [])

    def test_stationary_animal_is_only_observed(self):
        monitor = GuardianMonitor(guardian_config())
        events = drive(monitor, lambda t: [box(0.3, 0.5, 0.15)], 10)
        self.assertEqual(monitor.snapshot()[0]["movement"]["movement"], "stationary")
        self.assertEqual(events, [])

    def test_sudden_direction_change_is_reported(self):
        monitor = GuardianMonitor(guardian_config())
        # walks left for 4 s, then turns sharply right toward the back door
        script = lambda t: [box(0.5 - 0.06 * t if t < 4 else 0.26 + 0.09 * (t - 4), 0.75, 0.12, label="dog")]
        events = drive(monitor, script, 7)
        reasons = {code for event in events for code in event.reason_codes}
        self.assertIn("sudden_direction_change", reasons)
        self.assertIn("turned_toward", reasons)


class ZonePersonAndMultiAnimalTests(unittest.TestCase):
    def test_approach_to_protected_zone_then_inside(self):
        monitor = GuardianMonitor(guardian_config())
        events = drive(monitor, lambda t: [box(0.2 + 0.06 * t, 0.75, 0.12, label="dog")], 10)
        self.assertEqual(levels(events)[0], "approaching")
        self.assertIn("elevated_concern", levels(events))
        zone_events = [e for e in events if e.zone_id == "back_door"]
        self.assertTrue(zone_events)
        self.assertIn("Back door", zone_events[0].summary)
        final = {f["code"] for f in monitor.snapshot()[0]["assessment"]["factors"]}
        self.assertIn("in_protected_zone", final)
        self.assertIn("sustained_approach", final)
        self.assertIn("could be a dog, coyote or wolf", events[0].summary)

    def test_multiple_animals_are_tracked_separately(self):
        monitor = GuardianMonitor(guardian_config())
        script = lambda t: [box(0.2 + 0.06 * t, 0.75, 0.12), box(0.3, 0.35, 0.1)]
        events = drive(monitor, script, 8)
        tracks = monitor.snapshot()
        self.assertEqual(len({t["guardian_track_id"] for t in tracks}), 2)
        self.assertEqual({e.details["guardian_track_id"] for e in events}, {tracks[0]["guardian_track_id"]})

    def test_person_and_animal_together(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        script = lambda t: [box(0.15 + 0.05 * t, 0.7, 0.14), box(0.75, 0.7, 0.25, label="person", aspect=0.35)]
        events = drive(monitor, script, 8)
        self.assertIn("closing_on_person", {c for e in events for c in e.reason_codes})
        self.assertEqual(levels(events)[-1], "urgent_review")
        self.assertEqual(events[-1].severity, "high")
        self.assertEqual(events[-1].details["notify"], "always")

    def test_generated_text_never_claims_intent(self):
        monitor = GuardianMonitor(guardian_config())
        script = lambda t: [box(0.15 + 0.06 * t, 0.75, 0.14), box(0.8, 0.7, 0.25, label="person", aspect=0.35)]
        events = drive(monitor, script, 10)
        self.assertTrue(events)
        for event in events:
            text = (event.summary + " " + " ".join(f["text"] for f in event.details["assessment"]["factors"])).lower()
            for word in FORBIDDEN_CLAIMS:
                self.assertIsNone(re.search(rf"\b{word}\b", text), f"{word!r} in {text!r}")


class TrackingContinuityTests(unittest.TestCase):
    def test_loss_and_reacquisition_keeps_the_guardian_id(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        # occluded between 3 s and 7 s (longer than the tracker's 2.5 s), then reappears on its path
        script = lambda t: [] if 3 <= t < 7 else [box(0.1 + 0.05 * t, 0.5, 0.12)]
        drive(monitor, script, 10)
        tracks = monitor.snapshot()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["reacquired"], 1)

    def test_ambiguous_reacquisition_starts_a_new_guardian_track(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        script = lambda t: [box(0.5, 0.5, 0.12)] if t < 3 else ([] if t < 6 else [box(0.45, 0.5, 0.12), box(0.58, 0.52, 0.12)])
        drive(monitor, script, 8)
        live = [t for t in monitor.snapshot() if not t["lost"]]
        self.assertEqual(len(live), 2)
        self.assertTrue(all(t["reacquired"] == 0 for t in live))
        self.assertTrue(any(t["ambiguous_with"] for t in live))

    def test_tracker_jump_resets_kinematics_instead_of_inventing_a_dash(self):
        from sentinel.models import Track

        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        track = Track("cam_a:1", "cam_a", "animal", (0.25, 0.4, 0.13, 0.1), 0.9, NOON_MS, NOON_MS, confirmed=True, kind="bear")
        for step in range(15):
            now = NOON_MS + step * 200
            x = 0.25 if step < 10 else 0.6  # a box swap moves the "same" track 3.5 body heights in 200 ms
            track.bbox, track.last_seen_ms = (x, 0.4, 0.13, 0.1), now
            monitor.process("cam_a", now, [track], [])
        gtrack = next(iter(monitor.tracks.values()))
        self.assertEqual(gtrack.resets, 1)
        self.assertEqual(len(gtrack.samples), 5)
        self.assertNotEqual(gtrack.movement.movement, "lateral")


class SuppressionAndPerspectiveTests(unittest.TestCase):
    def test_low_confidence_flicker_does_not_alert(self):
        monitor = GuardianMonitor(guardian_config())
        script = lambda t: [box(0.4, 0.5 + 0.03 * t, 0.08 * (1.2 ** t), confidence=0.3)] if int(t * 5) % 3 else []
        self.assertEqual(drive(monitor, script, 8), [])

    def test_each_level_is_emitted_once_per_track(self):
        monitor = GuardianMonitor(guardian_config())
        events = drive(monitor, lambda t: [box(min(0.82, 0.2 + 0.06 * t), 0.75, 0.12, label="dog")], 30)
        self.assertEqual(len(levels(events)), len(set(levels(events))))

    def test_birds_are_ignored_by_default(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        self.assertEqual(drive(monitor, lambda t: [box(0.4, 0.5, 0.05 * (1.25 ** t), label="bird")], 6), [])
        self.assertEqual(monitor.snapshot(), [])

    def test_coherent_motion_of_everything_is_treated_as_camera_movement(self):
        monitor = GuardianMonitor(guardian_config(protected_zones=[]))
        script = lambda t: [box(0.1 + 0.07 * t, 0.7, 0.14), box(0.3 + 0.07 * t, 0.7, 0.14, label="person", aspect=0.35)]
        events = drive(monitor, script, 6)
        self.assertEqual(events, [])
        self.assertIn("camera_motion_suspected", monitor.snapshot()[0]["assessment"]["held_back"])

    def test_moving_camera_caps_every_level(self):
        config = guardian_config(protected_zones=[])
        config["cameras"][0]["guardian"] = {"moving_camera": True}
        monitor = GuardianMonitor(config)
        events = drive(monitor, lambda t: [box(0.4, 0.5 + 0.03 * t, 0.08 * (1.2 ** t))], 7)
        self.assertEqual(events, [])
        self.assertIn("camera_moves", monitor.snapshot()[0]["assessment"]["held_back"])

    def test_disabled_guardian_does_nothing(self):
        monitor = GuardianMonitor(make_config())
        self.assertEqual(drive(monitor, lambda t: [box(0.4, 0.5 + 0.03 * t, 0.08 * (1.2 ** t))], 7), [])


class CalibrationTests(unittest.TestCase):
    IMAGE = [[0.1, 0.9], [0.9, 0.9], [0.7, 0.5], [0.3, 0.5]]
    GROUND = [[-3, 2], [3, 2], [3, 14], [-3, 14]]

    def test_homography_round_trip(self):
        calibration = GroundCalibration.fit(self.IMAGE, self.GROUND)
        for (x, y), (gx, gy) in zip(self.IMAGE, self.GROUND):
            ground = calibration.to_ground((x, y))
            self.assertAlmostEqual(ground[0], gx, places=6)
            self.assertAlmostEqual(ground[1], gy, places=6)
        self.assertFalse(calibration.verified)  # four points cannot check themselves

    def test_metres_and_time_to_zone_only_with_calibration(self):
        script = lambda t: [box(0.3 + 0.05 * t, 0.75, 0.12, label="dog")]
        plain = GuardianMonitor(guardian_config())
        drive(plain, script, 6)
        self.assertNotIn("distance_to_zone_m", plain.snapshot()[0]["estimates"])
        self.assertFalse(plain.snapshot()[0]["estimates"]["calibrated"])

        config = guardian_config()
        config["cameras"][0]["guardian"] = {"calibration": {"image_points": self.IMAGE, "ground_points_m": self.GROUND}}
        calibrated = GuardianMonitor(config)
        drive(calibrated, script, 6)
        estimates = calibrated.snapshot()[0]["estimates"]
        self.assertTrue(estimates["calibrated"])
        self.assertIn("distance_to_zone_m", estimates)
        self.assertIn("time_to_zone_s", estimates)
        self.assertIn("unverified", estimates["calibration"])
        self.assertIn("flat ground", estimates["assumptions"])


class ConfigTests(unittest.TestCase):
    def test_valid_guardian_config(self):
        config = guardian_config()
        config["cameras"][0]["guardian"] = {"calibration": {"image_points": CalibrationTests.IMAGE, "ground_points_m": CalibrationTests.GROUND}}
        validate_config(config)

    def test_invalid_guardian_config(self):
        config = guardian_config(protected_zones=["nowhere"], notify_min_level="observed", species={"bear": {"concern": "extreme"}}, bogus=1)
        config["cameras"][0]["guardian"] = {"calibration": {"image_points": [[0, 0]], "ground_points_m": [[0, 0]]}}
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(config)
        text = str(caught.exception)
        for fragment in ("nowhere", "notify_min_level", "concern", "bogus", "calibration"):
            self.assertIn(fragment, text)


class AuditRegressionTests(unittest.TestCase):
    """Failures found by driving the monitor with messier input than the first tests."""

    PERSON = staticmethod(lambda: box(0.75, 0.8, 0.25, label="person", aspect=0.35))

    def no_zones(self, **extra):
        return GuardianMonitor(guardian_config(protected_zones=[], protected_zone_types=[], approach_camera=False, **extra))

    def test_detection_dropouts_do_not_silence_alerts(self):
        for keep in (lambda step: True, lambda step: step % 5 != 4, lambda step: step % 2 == 0):
            monitor = self.no_zones()
            events, tracker = [], IoUTracker("cam_a")
            for step in range(8 * FPS):
                now = NOON_MS + step * 1000 // FPS
                detections = [box(0.1 + 0.07 * step / FPS, 0.8, 0.15)] + ([self.PERSON()] if keep(step) else [])
                active, ended = tracker.update(detections, now)
                events += monitor.process("cam_a", now, active, ended)
            self.assertEqual(levels(events), ["elevated_concern", "urgent_review"])

    def test_person_walking_up_to_a_still_animal_is_not_reported_as_the_animal_moving(self):
        monitor = self.no_zones()
        events = drive(monitor, lambda t: [box(0.25, 0.8, 0.2, label="horse"), box(0.9 - 0.07 * t, 0.8, 0.25, label="person", aspect=0.35)], 7)
        self.assertTrue(events)
        for event in events:
            self.assertNotIn("moving toward", event.summary)
            self.assertIn("person_nearing", event.reason_codes)
            self.assertNotIn("closing_on_person", event.reason_codes)

    def test_species_wording_follows_the_track_not_one_frame(self):
        monitor = self.no_zones()
        events, tracker = [], IoUTracker("cam_a")
        for step in range(8 * FPS):
            now = NOON_MS + step * 1000 // FPS
            label = "dog" if step % 3 == 0 else "bear"
            active, ended = tracker.update([box(0.1 + 0.07 * step / FPS, 0.8, 0.15, label=label), self.PERSON()], now)
            events += monitor.process("cam_a", now, active, ended)
        self.assertEqual(levels(events)[-1], "urgent_review")
        for event in events:
            self.assertIn("detector labels: bear", event.summary)
            self.assertEqual(event.details["species"]["concern"], "high")  # the higher concern of the two labels
        self.assertEqual(monitor.snapshot()[0]["kind"], "bear")

    def test_high_concern_animal_staying_in_view_reaches_elevated_concern(self):
        monitor = self.no_zones()
        events = drive(monitor, lambda t: [box(0.3, 0.7, 0.18)], 70)
        self.assertEqual(levels(events), ["elevated_concern"])
        self.assertIn("lingering", events[0].reason_codes)
        self.assertIn("stayed in view", events[0].summary)
        cat = self.no_zones()
        self.assertEqual(drive(cat, lambda t: [box(0.3, 0.7, 0.08, label="cat")], 70), [])  # only high-concern animals

    def test_event_inside_a_protected_zone_names_the_zone(self):
        monitor = GuardianMonitor(guardian_config(approach_camera=False))
        events = drive(monitor, lambda t: [box(min(0.8, 0.4 + 0.1 * t), 0.8, 0.06, label="cat")], 65)
        inside = [e for e in events if "in_protected_zone" in e.reason_codes]
        self.assertTrue(inside)
        self.assertTrue(all(e.zone_id == "back_door" for e in inside))

    def test_restarted_worker_never_continues_an_old_animal(self):
        monitor = self.no_zones()
        first = IoUTracker("cam_a")
        drive(monitor, lambda t: [box(0.4, 0.5 + 0.03 * t, 0.08 * (1.2 ** t))], 6, tracker=first)
        bear_id = monitor.snapshot()[0]["guardian_track_id"]
        second = IoUTracker("cam_a", run=2)
        drive(monitor, lambda t: [box(0.8, 0.4, 0.06, label="cat")], 3, start=NOON_MS + 7000, tracker=second)
        cat = [g for g in monitor.snapshot() if g["kind"] == "cat"]
        self.assertEqual(len(cat), 1)
        self.assertNotEqual(cat[0]["guardian_track_id"], bear_id)
        self.assertTrue(cat[0]["track_id"].startswith("cam_a:r2-"))
        # The old run's track was never reported as ended; it is dropped once stale.
        drive(monitor, lambda t: [box(0.8, 0.4, 0.06, label="cat")], 12, start=NOON_MS + 10_000, tracker=second)
        self.assertNotIn(bear_id, {g["guardian_track_id"] for g in monitor.snapshot()})

    def test_replacement_monitor_continues_guardian_numbering(self):
        monitor = self.no_zones()
        drive(monitor, lambda t: [box(0.4, 0.5, 0.1)], 2)
        replacement = GuardianMonitor(guardian_config(), next_id=monitor.next_id)
        drive(replacement, lambda t: [box(0.4, 0.5, 0.1)], 2)
        self.assertNotEqual(monitor.snapshot()[0]["guardian_track_id"], replacement.snapshot()[0]["guardian_track_id"])

    def test_calibration_is_verified_only_when_held_out_marks_agree(self):
        exact = GroundCalibration.fit(CalibrationTests.IMAGE, CalibrationTests.GROUND)
        extra_image = [0.5, 0.7]
        extra_ground = list(exact.to_ground(tuple(extra_image)))
        good = GroundCalibration.fit(CalibrationTests.IMAGE + [extra_image], CalibrationTests.GROUND + [extra_ground])
        self.assertTrue(good.verified)
        self.assertEqual(good.status, "verified")
        bad = GroundCalibration.fit(CalibrationTests.IMAGE + [extra_image], CalibrationTests.GROUND + [[extra_ground[0] + 3, extra_ground[1]]])
        self.assertFalse(bad.verified)  # a mis-measured mark the least-squares fit absorbs
        self.assertIn("leave-one-out", bad.status)

    def test_config_type_errors_are_reported(self):
        config = guardian_config(ignore_kinds="bird", movement={"window_s": "long", "bogus": 1}, risk={"elevated_score": 7})
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(config)
        text = str(caught.exception)
        for fragment in ("ignore_kinds", "movement.window_s", "movement has unknown keys", "scores must rise"):
            self.assertIn(fragment, text)

    def test_guardian_health_component_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = SentinelRuntime(guardian_config(), storage_dir=folder, notifiers=[CallbackNotifier("capture", lambda n: None)])
            try:
                process = runtime.guardian.process
                runtime.guardian.process = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
                runtime.process_camera_update("cam_a", NOON_MS, [], [])
                self.assertEqual(runtime.health.components["guardian"]["state"], "degraded")
                runtime.guardian.process = process
                runtime.process_camera_update("cam_a", NOON_MS + 200, [], [])
                self.assertEqual(runtime.health.components["guardian"]["state"], "ok")
            finally:
                runtime.dispatcher.stop()
                runtime.close()


class FakeRecorder:
    def __init__(self):
        self.triggers, self.snapshots = [], []

    def snapshot(self, image, timestamp_ms, event_id):
        self.snapshots.append(event_id)
        return ""

    def trigger(self, incident_id, timestamp_ms):
        self.triggers.append(incident_id)
        return f"clips/{incident_id}.mp4"

    def clip_started_ms(self):
        return 0

    def finish(self):
        pass


class RuntimeIntegrationTests(unittest.TestCase):
    def test_alert_and_video_evidence_through_the_runtime(self):
        import numpy as np

        notifications = []
        with tempfile.TemporaryDirectory() as folder:
            runtime = SentinelRuntime(guardian_config(), storage_dir=folder, notifiers=[CallbackNotifier("capture", notifications.append)])
            recorder = FakeRecorder()
            runtime.recorders["cam_a"] = recorder
            tracker = IoUTracker("cam_a")
            image = np.zeros((90, 160, 3), dtype=np.uint8)
            events = []
            try:
                for step in range(10 * FPS):
                    now = NOON_MS + step * 1000 // FPS
                    active, ended = tracker.update([box(0.2 + 0.06 * step / FPS, 0.75, 0.12)], now)
                    events += runtime.process_camera_update("cam_a", now, active, ended, image=image)
                runtime.dispatcher.stop()
                wildlife = [e for e in events if e.event_type.startswith("wildlife_")]
                self.assertTrue(wildlife)
                self.assertNotIn("category_alerts_off", wildlife[-1].reason_codes)  # animal alerts are off by default
                self.assertTrue(recorder.triggers, "video clip (with pre-roll) must be recorded, not only a snapshot")
                self.assertTrue(any(e.clip_path for e in wildlife))
                self.assertEqual(len(notifications), len({n.severity for n in notifications}))  # once per level rise
                self.assertTrue(notifications)
                self.assertTrue(notifications[-1].message.endswith(DISCLAIMER))
                stored = runtime.store.list_events(limit=100)
                self.assertTrue(any(e["event_type"].startswith("wildlife_") for e in stored))
                guardian_state = runtime.snapshot_state()["guardian"]
                self.assertTrue(guardian_state["enabled"])
                self.assertTrue(guardian_state["tracks"][0]["trail"])
                self.assertTrue(runtime.guardian_overlays("cam_a"))
            finally:
                runtime.close()

    def test_security_events_unchanged_when_guardian_disabled(self):
        config = make_config(rules=[{"id": "shed_entry", "type": "zone_entry", "zones": ["shed"], "severity": "medium"}])
        scenario = Scenario.from_dict({"name": "walk", "duration_s": 12, "loop": False, "actors": [
            {"id": "p", "label": "person", "path": [{"t": 0, "camera": "cam_a", "x": 0.2, "y": 0.6}, {"t": 10, "camera": "cam_a", "x": 0.8, "y": 0.6}, {"t": 12, "camera": "cam_a", "x": 0.8, "y": 0.6}]}]})
        baseline = run_scenario(config, scenario, tail_s=5)
        enabled = copy.deepcopy(config) | {"guardian": {"enabled": True}}
        with_guardian = run_scenario(enabled, scenario, tail_s=5)
        strip = lambda result: [(e["event_type"], e["severity"], e["zone_id"]) for e in result["events"]]
        self.assertEqual(strip(baseline), strip(with_guardian))
        self.assertTrue(any(e[0] == "zone_entry" for e in strip(baseline)))


class ScenarioReplayTests(unittest.TestCase):
    def test_bear_approach_scenario(self):
        root = Path(__file__).resolve().parents[1]
        from sentinel.config import load_config

        config = load_config(root / "config" / "sentinel.guardian.example.json")
        result = run_scenario(config, Scenario.load(root / "config" / "scenarios" / "wildlife_bear_approach.json"), tail_s=10)
        types = [e["event_type"] for e in result["events"] if e["event_type"].startswith("wildlife_")]
        self.assertIn("wildlife_urgent_review", types)
        self.assertTrue(result["notifications"])
        self.assertTrue(all("species not determined" in e["summary"] for e in result["events"] if e["event_type"].startswith("wildlife_")))


if __name__ == "__main__":
    unittest.main()
