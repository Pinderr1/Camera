"""Household-member recognition: consent-based enrollment, cautious statuses,
recognition zones, rule filters, approach rules, privacy, and the runtime worker.
Unit tests use a fake face model; RealModelTests use the downloaded YuNet/SFace
models when present (python scripts/fetch_face_models.py)."""

from __future__ import annotations

import copy
import json
import math
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinel.config import ConfigValidationError, validate_config
from sentinel.engine import TemporalEventEngine
from sentinel.identity.gallery import CONSENT_STATEMENT_VERSION, EnrollmentError, FaceGallery, validate_consent
from sentinel.identity.matching import Evidence, FaceObservation, decide, judge_scores, normalize
from sentinel.identity.monitor import IdentityMonitor
from sentinel.identity.settings import IdentitySettings
from sentinel.models import SecurityEvent
from sentinel.tracker import IoUTracker, TrackerConfig

from sentinel_helpers import NOON_MS, Driver, make_config, person

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = IdentitySettings(enabled=True, zones=("yard", "shed"))
CONSENT = {"agreed": True, "given_by": "Alex", "relationship": "self", "method": "in_person", "statement_version": CONSENT_STATEMENT_VERSION}


def direction(seed: int, noise: float = 0.0, rng: random.Random | None = None) -> list[float]:
    """A stable 128-value 'face template' per seed; `noise` makes a different photo of the same face."""
    base = random.Random(seed)
    vector = [base.gauss(0, 1) for _ in range(128)]
    if noise:
        rng = rng or random.Random(seed * 7 + 1)
        vector = [value + rng.gauss(0, noise) for value in vector]
    return normalize(vector)


def identity_config(rules=None, **identity):
    config = make_config(rules)
    config["identity"] = {"enabled": True, "zones": ["yard", "shed"]} | identity
    return config


def plain_gallery(folder) -> FaceGallery:
    return FaceGallery(Path(folder) / "identity" / "gallery.dat")


class MatchingTests(unittest.TestCase):
    def test_one_comparison_is_judged_with_a_margin_and_a_grey_band(self):
        self.assertEqual(judge_scores({"a": 0.7, "b": 0.2}, SETTINGS).outcome, "match")
        self.assertEqual(judge_scores({"a": 0.5, "b": 0.47}, SETTINGS).outcome, "uncertain", "two members too close to call")
        self.assertEqual(judge_scores({"a": 0.38}, SETTINGS).outcome, "uncertain", "between unknown_below and the threshold")
        self.assertEqual(judge_scores({"a": 0.1}, SETTINGS).outcome, "no_match")

    def test_status_needs_several_agreeing_observations(self):
        evidence = Evidence()
        for _ in range(SETTINGS.min_agreeing - 1):
            evidence.add(FaceObservation("match", "a", 0.7))
        self.assertEqual(decide(evidence, SETTINGS)[0], "unverified", "one or two matches decide nothing")
        evidence.add(FaceObservation("match", "a", 0.7))
        self.assertEqual(decide(evidence, SETTINGS)[:2], ("known", "a"))

    def test_conflicting_members_and_no_face_stay_unverified(self):
        evidence = Evidence()
        for member in ("a", "b") * SETTINGS.min_agreeing:
            evidence.add(FaceObservation("match", member, 0.6))
        self.assertEqual(decide(evidence, SETTINGS), ("unverified", "", "conflicting_matches"))
        faceless = Evidence()
        for _ in range(10):
            faceless.add(FaceObservation("no_face"))
        self.assertEqual(decide(faceless, SETTINGS), ("unverified", "", "no_usable_face"), "a hidden face is never 'unknown'")

    def test_unknown_requires_clear_non_matches(self):
        evidence = Evidence()
        for _ in range(SETTINGS.min_unknown):
            evidence.add(FaceObservation("no_match", score=0.1))
        self.assertEqual(decide(evidence, SETTINGS)[0], "unknown")
        evidence.add(FaceObservation("match", "a", 0.6))
        evidence.add(FaceObservation("match", "a", 0.6))
        self.assertEqual(decide(evidence, SETTINGS)[0], "unverified", "mixed evidence says nothing")


class GalleryTests(unittest.TestCase):
    def test_consent_is_required_and_recorded(self):
        for missing in ("agreed", "given_by", "relationship", "method", "statement_version"):
            with self.subTest(missing=missing), self.assertRaises(EnrollmentError):
                validate_consent({k: v for k, v in CONSENT.items() if k != missing})
        with self.assertRaises(EnrollmentError):
            validate_consent(CONSENT | {"agreed": False})
        with self.assertRaises(EnrollmentError):
            validate_consent(CONSENT | {"statement_version": "old"})
        record = validate_consent(CONSENT | {"relationship": "parent_or_guardian", "given_by": "Sam (parent)"})
        self.assertEqual(record["relationship"], "parent_or_guardian")

    def test_enrollment_checks_consistency_and_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            gallery = plain_gallery(folder)
            alex = gallery.add_member("Alex", CONSENT, "owner", NOON_MS)
            self.assertEqual(alex["consent"]["recorded_by"], "owner")
            self.assertNotIn("templates", alex)
            with self.assertRaises(EnrollmentError, msg="photos of two different people"):
                gallery.add_templates(alex["member_id"], [direction(1, 0.3), direction(2, 0.3)], SETTINGS, NOON_MS)
            gallery.add_templates(alex["member_id"], [direction(1, 0.3, random.Random(i)) for i in range(3)], SETTINGS, NOON_MS)
            self.assertEqual(gallery.get(alex["member_id"])["photos_enrolled"], 3)
            sam = gallery.add_member("Sam", CONSENT | {"given_by": "Sam"}, "owner", NOON_MS)
            with self.assertRaises(EnrollmentError, msg="Alex's face enrolled again under another name"):
                gallery.add_templates(sam["member_id"], [direction(1, 0.3, random.Random(9))], SETTINGS, NOON_MS)
            with self.assertRaises(EnrollmentError):
                gallery.add_member("alex", CONSENT, "owner", NOON_MS)
            self.assertEqual(set(gallery.templates(SETTINGS)), {alex["member_id"]}, "Sam has no photos yet: not matched")

    def test_file_is_protected_and_removal_deletes_templates(self):
        with tempfile.TemporaryDirectory() as folder:
            gallery = plain_gallery(folder)
            member = gallery.add_member("Alexandra", CONSENT, "owner", NOON_MS)
            gallery.add_templates(member["member_id"], [direction(3, 0.2, random.Random(i)) for i in range(3)], SETTINGS, NOON_MS)
            raw = (Path(folder) / "identity" / "gallery.dat").read_bytes()
            import sys

            if sys.platform == "win32":
                self.assertNotIn(b"Alexandra", raw, "DPAPI-encrypted on Windows")
            reopened = plain_gallery(folder)
            self.assertEqual(reopened.members()[0]["display_name"], "Alexandra")
            removed = reopened.remove_member(member["member_id"])
            self.assertEqual(removed["templates_deleted"], 3)
            self.assertEqual(plain_gallery(folder).members(), [])
            self.assertEqual(plain_gallery(folder).templates(SETTINGS), {})

    def test_unreadable_gallery_pauses_recognition_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "identity" / "gallery.dat"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not a gallery")
            gallery = FaceGallery(path)
            self.assertTrue(gallery.error)
            self.assertEqual(gallery.templates(SETTINGS), {})
            with self.assertRaises(EnrollmentError):
                gallery.add_member("Alex", CONSENT, "owner", NOON_MS)
            self.assertEqual(path.read_bytes(), b"not a gallery", "evidence of the old file is kept")


class ValidationTests(unittest.TestCase):
    def test_valid_and_off_by_default(self):
        validate_config(identity_config())
        validate_config(make_config())
        self.assertFalse(IdentitySettings.from_dict(None).enabled)

    def test_recognition_needs_zones_and_never_public_areas(self):
        config = identity_config()
        config["identity"]["zones"] = []
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(config)
        self.assertIn("at least one recognition zone", str(caught.exception))
        config = identity_config()
        config["cameras"][1]["zones"].append({"id": "street", "name": "Street", "type": "street", "polygon": [[0, 0], [1, 0], [1, 0.1]]})
        config["identity"]["zones"] = ["street"]
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(config)
        self.assertIn("never recognized in public", str(caught.exception))

    def test_numbers_keys_and_rule_fields(self):
        config = identity_config(match_threshold=2, min_agreeing=0.5, colour=1, unknown_below=0.9)
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(config)
        text = str(caught.exception)
        for fragment in ("match_threshold", "min_agreeing", "unknown keys"):
            self.assertIn(fragment, text)
        rule = {"id": "r", "type": "zone_entry", "severity": "high", "identity_filter": "everyone", "identity_wait_s": 60}
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(identity_config([rule]))
        self.assertIn("identity_filter", str(caught.exception))
        self.assertIn("identity_wait_s", str(caught.exception))
        with self.assertRaises(ConfigValidationError):
            validate_config(identity_config([{"id": "v", "type": "zone_entry", "severity": "low", "classes": ["vehicle"], "identity_filter": "not_known"}]))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = identity_config()
        self.config["cameras"][0]["zones"].append({"id": "lane", "name": "Lane", "type": "general", "polygon": [[0, 0], [1, 0], [1, 0.2], [0, 0.2]]})
        self.monitor = IdentityMonitor(self.config)
        self.monitor.members_ready = True
        self.monitor.names = {"m_a": "Alex"}
        self.tracker = IoUTracker("cam_a", TrackerConfig(min_hits=1))

    def step(self, now, *detections, sites=None):
        active, ended = self.tracker.update(list(detections), now)
        self.monitor.observe("cam_a", now, active, ended, sites or {track.track_id: "site_1" for track in active})
        return active

    def test_requests_only_people_inside_recognition_zones(self):
        self.step(NOON_MS, person(0.3, 0.6), person(0.8, 0.15, label="vehicle", size=(0.1, 0.1)))
        self.assertEqual(len(self.monitor.requests("cam_a", NOON_MS)), 1, "the person in the yard; never the vehicle")
        self.assertEqual(self.monitor.requests("cam_a", NOON_MS), [], "pending: one check at a time per track")
        monitor = IdentityMonitor(identity_config(zones=["shed"]))
        monitor.members_ready = True
        tracker = IoUTracker("cam_a", TrackerConfig(min_hits=1))
        active, _ = tracker.update([person(0.3, 0.6)], NOON_MS)  # ground point in the yard, which is not a recognition zone here
        monitor.observe("cam_a", NOON_MS, active, [], {active[0].track_id: "site_9"})
        self.assertEqual(monitor.requests("cam_a", NOON_MS), [])
        self.assertEqual(monitor.site_identity("site_9")["reason"], "outside_recognition_zones")

    def test_nothing_is_requested_without_enrolled_members(self):
        self.monitor.members_ready = False
        self.step(NOON_MS, person(0.3, 0.6))
        self.assertEqual(self.monitor.requests("cam_a", NOON_MS), [])
        self.assertEqual(self.monitor.site_identity("site_1")["reason"], "no_members_enrolled")

    def test_status_change_is_reported_once_and_forgotten_on_withdrawal(self):
        changes, checked = [], set()
        now = NOON_MS
        for _ in range(6):
            now += 600
            self.step(now, person(0.3, 0.6))
            for track_id, _ in self.monitor.requests("cam_a", now):
                checked.add(track_id)
                changes += self.monitor.result(track_id, FaceObservation("match", "m_a", 0.7, 0.4, 60, now))
        self.assertEqual([c["identity"]["status"] for c in changes], ["known"])
        self.assertEqual(changes[0]["identity"]["display_name"], "Alex")
        self.assertEqual(self.monitor.overlays("cam_a"), {checked.pop(): "Alex"})
        forgotten = self.monitor.forget_member("m_a")
        self.assertEqual(forgotten[0]["identity"]["status"], "unverified")
        self.assertEqual(self.monitor.site_identity("site_1")["member_id"], "")

    def test_linked_camera_tracks_add_up_and_conflicts_unverify(self):
        monitor = IdentityMonitor(self.config)
        monitor.members_ready = True
        tracker_b = IoUTracker("cam_b", TrackerConfig(min_hits=1))
        active_a = self.step(NOON_MS, person(0.3, 0.6), sites={})
        monitor.observe("cam_a", NOON_MS, active_a, [], {active_a[0].track_id: "site_7"})
        active_b, _ = tracker_b.update([person(0.5, 0.6)], NOON_MS)
        monitor.observe("cam_b", NOON_MS, active_b, [], {active_b[0].track_id: "site_7"})
        monitor.result(active_a[0].track_id, FaceObservation("match", "m_a", 0.7))
        monitor.result(active_a[0].track_id, FaceObservation("match", "m_a", 0.7))
        self.assertEqual(monitor.site_identity("site_7")["status"], "unverified")
        monitor.result(active_b[0].track_id, FaceObservation("match", "m_a", 0.7))
        self.assertEqual(monitor.site_identity("site_7")["status"], "known", "evidence from the linked camera counts")
        for _ in range(3):
            monitor.result(active_b[0].track_id, FaceObservation("match", "m_b", 0.7))
        self.assertEqual(monitor.site_identity("site_7")["reason"], "conflicting_matches")

    def test_rechecks_are_spaced(self):
        now = NOON_MS
        self.step(now, person(0.3, 0.6))
        (track_id, _), = self.monitor.requests("cam_a", now)
        self.monitor.result(track_id, FaceObservation("no_face"))
        now += 100
        self.step(now, person(0.3, 0.6))
        self.assertEqual(self.monitor.requests("cam_a", now), [], f"not again within recheck_s ({SETTINGS.recheck_s} s)")
        now += 500
        self.step(now, person(0.3, 0.6))
        self.assertEqual(len(self.monitor.requests("cam_a", now)), 1)


class EngineIdentityTests(unittest.TestCase):
    RULE = {"id": "shed_entry", "type": "zone_entry", "zones": ["shed"], "severity": "high", "identity_filter": "not_known", "identity_wait_s": 2}

    def drive(self, status_of, rules=None, seconds=8):
        engine = TemporalEventEngine(identity_config(rules or [self.RULE]))
        engine.identity_lookup = status_of
        driver = Driver(engine)
        driver.run(seconds, {"cam_a": lambda now: [person(0.75, 0.6)]})
        return driver

    def test_recognized_member_is_recorded_without_alerting(self):
        driver = self.drive(lambda site: {"status": "known", "member_id": "m_a", "display_name": "Alex", "reason": "consistent_matches", "deciding": False})
        (event,) = driver.of_type("zone_entry")
        self.assertEqual(event.severity, "info")
        self.assertIn("recognized_household_member", event.reason_codes)
        self.assertEqual(event.details["original_severity"], "high")
        self.assertEqual(event.details["identity"]["status"], "known")

    def test_unrecognized_or_unverified_people_still_alert(self):
        for status in ("unknown", "unverified"):
            with self.subTest(status=status):
                driver = self.drive(lambda site, s=status: {"status": s, "member_id": "", "display_name": "", "reason": "x", "deciding": False})
                (event,) = driver.of_type("zone_entry")
                self.assertEqual(event.severity, "high")
                self.assertNotIn("recognized_household_member", event.reason_codes)

    def test_entry_waits_briefly_while_recognition_is_deciding(self):
        state = {"status": "unverified", "member_id": "", "display_name": "", "reason": "insufficient_evidence", "deciding": True}
        engine = TemporalEventEngine(identity_config([self.RULE]))
        engine.identity_lookup = lambda site: state
        driver = Driver(engine)
        driver.run(1.6, {"cam_a": lambda now: [person(0.75, 0.6)]})
        self.assertEqual(driver.of_type("zone_entry"), [], "still deciding within identity_wait_s")
        state.update(status="known", member_id="m_a", display_name="Alex", deciding=False)
        driver.run(1.0, {"cam_a": lambda now: [person(0.75, 0.6)]})
        (event,) = driver.of_type("zone_entry")
        self.assertEqual(event.severity, "info", "recognized within the wait: recorded, no alert")
        state.update(status="unverified", deciding=True)
        driver2 = Driver(TemporalEventEngine(identity_config([self.RULE])))
        driver2.engine.identity_lookup = lambda site: state
        driver2.run(4, {"cam_a": lambda now: [person(0.75, 0.6)]})
        (late,) = driver2.of_type("zone_entry")
        self.assertEqual(late.severity, "high", "the wait is bounded: a person still being checked alerts afterwards")

    def test_recognition_off_changes_nothing(self):
        engine = TemporalEventEngine(identity_config([self.RULE]))
        driver = Driver(engine)
        driver.run(8, {"cam_a": lambda now: [person(0.75, 0.6)]})
        (event,) = driver.of_type("zone_entry")
        self.assertEqual(event.severity, "high")
        self.assertNotIn("identity", event.details)


class ApproachRuleTests(unittest.TestCase):
    RULE = {"id": "near_shed", "type": "zone_approach", "zones": ["shed"], "severity": "medium", "within": 2.0, "min_closing": 1.0, "window_s": 4}

    def run_path(self, path, seconds=8):
        engine = TemporalEventEngine(make_config([self.RULE]))
        driver = Driver(engine)
        start = driver.now
        driver.run(seconds, {"cam_a": lambda now: [person(*path((now - start) / 1000))]})
        return driver.of_type("zone_approach")

    def test_walking_toward_the_shed_fires_once_before_entering(self):
        events = self.run_path(lambda t: (min(0.47, 0.1 + 0.05 * t), 0.6))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.zone_id, "shed")
        self.assertIn("toward Shed", event.summary + " ")  # "moved toward Shed (...)" or "moved close to Shed"
        self.assertLessEqual(event.details["distance_body_heights"], 2.0)
        self.assertGreaterEqual(event.details["closed_body_heights"], 1.0)
        for word in ("intruder", "suspicious", "trespass"):
            self.assertNotIn(word, event.summary.lower())

    def test_standing_still_walking_away_or_parallel_does_not_fire(self):
        self.assertEqual(self.run_path(lambda t: (0.4, 0.6)), [])
        self.assertEqual(self.run_path(lambda t: (0.45 - 0.04 * t, 0.6)), [])
        self.assertEqual(self.run_path(lambda t: (0.3, 0.3 + 0.05 * t)), [])

    def test_validation(self):
        validate_config(make_config([self.RULE]))
        with self.assertRaises(ConfigValidationError):
            validate_config(make_config([{k: v for k, v in self.RULE.items() if k != "zones"}]))
        with self.assertRaises(ConfigValidationError):
            validate_config(make_config([self.RULE | {"within": 50}]))


class FakeAnalyzer:
    """Stands in for YuNet + SFace: the crop's brightness picks the 'face'."""

    def __init__(self, *args, **kwargs):
        self.calls = 0

    def person_face(self, crop, settings):
        self.calls += 1
        level = int(crop.mean())
        if level < 30:
            return "no_face", None, 0
        import numpy as np

        seed = 1 if level < 150 else 2
        return "face", np.asarray(direction(seed, 0.2, random.Random(self.calls)), dtype=np.float32), 64

    def enrollment_template(self, image, settings):
        self.calls += 1
        return direction(1 if int(image.mean()) < 150 else 2, 0.2, random.Random(self.calls + 1000))


class RuntimeIdentityTests(unittest.TestCase):
    def runtime(self, folder, **identity):
        from sentinel.notify import CallbackNotifier
        from sentinel.runtime import SentinelRuntime

        self.notifications = []
        rules = [{"id": "shed_entry", "type": "zone_entry", "zones": ["shed"], "severity": "high", "identity_filter": "not_known", "identity_wait_s": 3}]
        config = identity_config(rules, recheck_s=0.2, **identity)
        return SentinelRuntime(config, storage_dir=folder, notifiers=[CallbackNotifier("capture", self.notifications.append)])

    def enroll(self, runtime, image_level=100):
        import numpy as np

        from sentinel.identity.service import Enrollment

        member = runtime.identity_gallery.add_member("Alex", CONSENT, "owner", NOON_MS)
        images = [np.full((200, 200, 3), image_level, dtype=np.uint8)] * 3
        templates = Enrollment(runtime.identity.settings, analyzer_factory=FakeAnalyzer).templates(images)
        runtime.identity_gallery.add_templates(member["member_id"], templates, runtime.identity.settings, NOON_MS)
        return member

    def walk(self, runtime, level, seconds=6.0):
        import numpy as np

        tracker = IoUTracker("cam_a", TrackerConfig(min_hits=2))
        image = np.full((360, 640, 3), level, dtype=np.uint8)
        events = []
        for step in range(int(seconds * 5)):
            now = NOON_MS + step * 200
            active, ended = tracker.update([person(0.75, 0.6)], now)
            events += runtime.process_camera_update("cam_a", now, active, ended, image=image)
            runtime.identity_frame("cam_a", now, image)
            time.sleep(0.03)
        time.sleep(0.3)
        return events, runtime.store.list_events(since_ms=NOON_MS - 1, limit=100)

    def test_enrolled_member_is_recognized_and_not_alerted(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sentinel.identity.service.FaceAnalyzer", FakeAnalyzer):
            runtime = self.runtime(folder)
            try:
                member = self.enroll(runtime)
                deadline = time.time() + 5
                while runtime.identity_service.status()["members_ready"] != 1 and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(runtime.identity.members_ready)
                _, stored = self.walk(runtime, level=100)
                identity = [e for e in stored if e["event_type"] == "identity"]
                self.assertEqual(len(identity), 1, [e["summary"] for e in stored])
                self.assertEqual(identity[0]["severity"], "info")
                self.assertNotIn("Alex", identity[0]["summary"], "event text never names anyone (boundary 1)")
                body = json.loads(json.dumps(identity[0]))
                self.assertEqual(body["details"]["identity"]["member_id"], member["member_id"])
                entry = [e for e in stored if e["event_type"] == "zone_entry"]
                self.assertEqual(entry[0]["severity"], "info")
                self.assertEqual(self.notifications, [], "a recognized household member's arrival does not push")
                self.assertEqual(runtime.health.components["identity"]["state"], "ok")
                state = runtime.snapshot_state()["identity"]
                self.assertGreaterEqual(state["worker"]["faces_checked"], 3)
                self.assertNotIn("template", json.dumps(state))
                # Consent withdrawn: templates gone, stored events scrubbed, audit written.
                removed = runtime.identity_member_removed(member["member_id"], "owner")
                self.assertGreaterEqual(removed["events_scrubbed"], 1)
                after = runtime.store.list_events(since_ms=NOON_MS - 1, limit=100)
                self.assertNotIn(member["member_id"], json.dumps(after))
                self.assertEqual(runtime.identity_gallery.members(), [])
            finally:
                runtime.close()

    def test_unrecognized_person_still_alerts_and_nothing_is_kept(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sentinel.identity.service.FaceAnalyzer", FakeAnalyzer):
            runtime = self.runtime(folder)
            try:
                self.enroll(runtime)
                deadline = time.time() + 5
                while not runtime.identity.members_ready and time.time() < deadline:
                    time.sleep(0.05)
                _, stored = self.walk(runtime, level=200)  # another face
                kinds = {e["event_type"]: e for e in stored}
                self.assertEqual(kinds["zone_entry"]["severity"], "high")
                self.assertTrue(self.notifications, "people who were not recognized still alert")
                for notification in self.notifications:
                    for word in ("intruder", "suspicious", "unknown person", "stranger"):
                        self.assertNotIn(word, notification.message.lower())
                gallery_bytes = (Path(folder) / "identity" / "gallery.dat").read_bytes()
                self.assertEqual(len(runtime.identity_gallery.templates(runtime.identity.settings)), 1, "nothing added for the other face")
                self.assertLess(len(gallery_bytes), 40_000)
            finally:
                runtime.close()

    def test_missing_models_degrade_to_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime(folder, detector_model=str(Path(folder) / "missing.onnx"))
            try:
                deadline = time.time() + 5
                while "identity" not in runtime.health.components and time.time() < deadline:
                    time.sleep(0.05)
                self.assertEqual(runtime.identity_service.state, "failed")
                self.assertEqual(runtime.health.components["identity"]["state"], "degraded")
                events, _ = self.walk(runtime, level=100, seconds=5)
                entry = [e for e in events if e.event_type == "zone_entry"]
                self.assertEqual(entry[0].severity, "high", "no recognition: nobody is 'known', alerts are unchanged")
            finally:
                runtime.close()

    def test_recognition_events_never_request_the_drone(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sentinel.identity.service.FaceAnalyzer", FakeAnalyzer):
            runtime = self.runtime(folder)
            try:
                with mock.patch.object(runtime, "_maybe_request_drone") as request:
                    entry = SecurityEvent(event_id="evt_1", event_type="zone_entry", severity="high", camera_id="cam_a", timestamp_ms=NOON_MS,
                                          started_ms=NOON_MS, confidence=0.9, summary="Person entered Shed", label="person", zone_id="shed",
                                          site_track_id="site_1")
                    runtime._handle_events([entry], NOON_MS)
                    self.assertEqual(request.call_count, 1, "an ordinary incident may request an inspection")
                    request.reset_mock()
                    runtime._identity_changes([{"site_track_id": "site_1", "track_id": "cam_a:1:1", "camera_id": "cam_a", "zone_id": "shed",
                                                "previous": "unverified", "identity": {"status": "unknown", "member_id": "", "display_name": "",
                                                                                       "reason": "no_enrolled_member_matched", "evidence": {}}}], NOON_MS)
                    request.assert_not_called()
            finally:
                runtime.close()

    def test_recognition_off_by_default_starts_nothing(self):
        from sentinel.runtime import SentinelRuntime

        with tempfile.TemporaryDirectory() as folder:
            runtime = SentinelRuntime(make_config(), storage_dir=folder, notifiers=[])
            try:
                self.assertIsNone(runtime.identity_service)
                self.assertIsNone(runtime.engine.identity_lookup)
                self.assertNotIn("identity", runtime.health.components)
                self.assertFalse((Path(folder) / "identity").exists(), "no gallery file until someone is enrolled")
            finally:
                runtime.close()


class ApiTests(unittest.TestCase):
    def test_enrollment_flow_and_no_biometrics_in_responses(self):
        import base64

        import numpy as np

        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI test client not installed")
        import cv2

        from sentinel.api import create_app
        from sentinel.service import SentinelService

        with tempfile.TemporaryDirectory() as folder, mock.patch("sentinel.identity.service.FaceAnalyzer", FakeAnalyzer):
            config = identity_config()
            config["storage"] = {"dir": folder}
            service = SentinelService(config, start_cameras=False)
            try:
                client = TestClient(create_app(service, api_token=None))
                status = client.get("/api/v1/identity").json()
                self.assertIn("statement", status["consent"])
                self.assertEqual(client.post("/api/v1/identity/members", json={"display_name": "Alex", "consent": CONSENT | {"agreed": False}}).status_code, 422)
                member = client.post("/api/v1/identity/members", json={"display_name": "Alex", "consent": CONSENT}).json()
                ok, jpeg = cv2.imencode(".jpg", np.full((240, 240, 3), 100, dtype=np.uint8))
                photo = base64.b64encode(jpeg.tobytes()).decode()
                reply = client.post(f"/api/v1/identity/members/{member['member_id']}/photos", json={"images": [photo] * 3})
                self.assertEqual(reply.status_code, 200, reply.text)
                self.assertTrue(reply.json()["enrollment_complete"])
                listing = client.get("/api/v1/identity").json()
                self.assertEqual(listing["members"][0]["photos_enrolled"], 3)
                self.assertNotIn("templates", listing["members"][0])
                self.assertNotIn("template", json.dumps(listing["members"] + listing["subjects"]))
                self.assertEqual(client.post(f"/api/v1/identity/members/{member['member_id']}/photos", json={"images": ["***"]}).status_code, 422)
                self.assertEqual(client.delete(f"/api/v1/identity/members/{member['member_id']}").status_code, 200)
                self.assertEqual(client.get("/api/v1/identity").json()["members"], [])
                audit = json.dumps(service.runtime.store.list_audit(limit=50))
                for action in ("identity_member_added", "identity_photos_enrolled", "identity_member_removed"):
                    self.assertIn(action, audit)
            finally:
                service.runtime.close()


class StandoffTests(unittest.TestCase):
    """The drone never flies to, over, or near a reported person."""

    def setUp(self):
        from sentinel.drone import DroneManager, Geofence
        from sentinel.drone.base import map_point_to_geo
        from sentinel.drone.manager import DroneSafetyConfig
        from sentinel.drone.mock import SimulatedDrone

        self.origin = (45.0, -75.0)
        self.geo = lambda x, y: map_point_to_geo(self.origin, x, y)
        fence = Geofence([list(self.geo(x, y)) for x, y in [[0, 0], [120, 0], [120, 80], [0, 80]]], 40, 5)
        self.drone = SimulatedDrone(self.geo(5, 40), cruise_speed_mps=15)
        self.drone.connect()
        self.manager = DroneManager(self.drone, fence, self.origin, DroneSafetyConfig(inspection_altitude_m=15, hover_s=2), home=self.geo(5, 40))

    def test_person_at_target_moves_the_hover_point_back(self):
        shed = [[100, 35], [110, 35], [110, 45], [100, 45]]
        request = self.manager.request_inspection(requested_by="sentinel", map_point=(105, 40), label="Shed", source="incident",
                                                  incident_id="inc_1", person_map_polygons=[shed])
        route = self.manager.plan(request)
        self.assertEqual(route.problems, [])
        from sentinel.drone.base import distance_m
        from sentinel.drone.standoff import PersonArea, distance_to_area_m

        area = PersonArea.from_dict(request.people_areas[0])
        self.assertGreaterEqual(distance_to_area_m(self.origin, area, *route.station), 30.0 - 0.5)
        self.assertGreater(distance_m(route.station, route.target), 30.0)
        inspect = next(w for w in route.waypoints if w["kind"] == "inspect")
        self.assertEqual((inspect["lat"], inspect["lon"]), route.station, "the exported plan hovers at the station, not over the person")
        self.assertTrue(any("hovers" in note for note in route.notes))

    def test_no_clear_point_or_route_past_people_is_refused(self):
        from sentinel.drone.base import FlightCommandRejected

        everywhere = [[0, 0], [120, 0], [120, 80], [0, 80]]
        request = self.manager.request_inspection(requested_by="sentinel", map_point=(105, 40), label="Shed", incident_id="inc_2",
                                                  person_map_polygons=[everywhere])
        self.assertIn("no_inspection_point_clear_of_people", self.manager.plan(request).problems)
        with self.assertRaises(FlightCommandRejected):
            self.manager.approve(request.request_id, "operator")
        self.manager.people_areas = lambda: [{"source": "camera", "label": "Path", "map_polygon": [[50, 38], [54, 38], [54, 42], [50, 42]]}]
        clear = self.manager.request_inspection(requested_by="op", map_point=(105, 40), label="Shed")
        self.assertIn("route_passes_near_people", self.manager.plan(clear).problems)

    def test_manual_goto_near_a_person_is_refused(self):
        from sentinel.drone.base import FlightCommandRejected

        self.manager.people_areas = lambda: [{"source": "camera", "label": "Yard", "map_polygon": [[60, 30], [70, 30], [70, 50], [60, 50]]}]
        self.manager.command("takeoff", "op", {"altitude_m": 15})
        lat, lon = self.geo(65, 40)
        with self.assertRaises(FlightCommandRejected) as caught:
            self.manager.command("goto", "op", {"lat": lat, "lon": lon, "altitude_m": 15})
        self.assertIn("target_near_person", str(caught.exception))

    def test_person_appearing_near_the_aircraft_stops_it(self):
        request = self.manager.request_inspection(requested_by="op", map_point=(100, 40), label="Gate")
        self.manager.approve(request.request_id, "op")
        for _ in range(200):
            self.manager.step(0.25)
            if request.phase == "enroute" and self.drone.telemetry().lat is not None:
                break
        for _ in range(8):
            self.manager.step(0.25)
        here = self.drone.telemetry()
        from sentinel.drone.base import geo_to_map_point

        x, y = geo_to_map_point(self.origin, here.lat, here.lon)
        self.manager.people_areas = lambda: [{"source": "camera", "label": "Lawn", "map_polygon": [[x + 5, y - 2], [x + 8, y - 2], [x + 8, y + 2], [x + 5, y + 2]]}]
        self.manager.step(0.25)
        self.assertEqual(self.manager.safety_state, "person_too_close")
        self.assertEqual(request.phase, "aborting")
        self.assertEqual(request.note, "person_within_standoff")
        self.assertIn("never descend", self.manager.advisories[-1]["text"].lower())
        held = self.drone.telemetry()
        for _ in range(8):
            self.manager.step(0.25)
        moved = math.dist(geo_to_map_point(self.origin, held.lat, held.lon), geo_to_map_point(self.origin, *self._pos()))
        self.assertLess(moved, 2.0, "holding position: it does not keep flying toward the person")

    def _pos(self):
        telemetry = self.drone.telemetry()
        return telemetry.lat, telemetry.lon

    def test_drone_video_sighting_counts_with_its_uncertainty(self):
        from sentinel.drone.standoff import PersonArea, distance_to_area_m

        area = PersonArea("drone_video", "person", point=self.geo(50, 40), radius_m=6)
        self.assertAlmostEqual(distance_to_area_m(self.origin, area, *self.geo(70, 40)), 14.0, delta=0.5)

    def test_standoff_cannot_go_below_the_floor(self):
        from sentinel.drone.manager import DroneSafetyConfig

        self.assertEqual(DroneSafetyConfig(person_standoff_m=2).standoff_m, 10.0)
        self.assertEqual(DroneSafetyConfig().standoff_m, 30.0)
        config = make_config()
        config["site"]["map"]["origin_geo"] = [45.0, -75.0]
        config["drone"] = {"enabled": True, "geofence": {"map_polygon": [[0, 0], [50, 0], [50, 50], [0, 50]], "max_altitude_m": 40},
                           "safety": {"person_standoff_m": 5}}
        with self.assertRaises(ConfigValidationError) as caught:
            validate_config(config)
        self.assertIn("person_standoff_m", str(caught.exception))


FACES = ROOT / "models" / "face"
MOT09 = ROOT / "data" / "bench" / "mot16" / "MOT16-09"


@unittest.skipUnless((FACES / "face_recognition_sface_2021dec.onnx").is_file(), "face models not downloaded (scripts/fetch_face_models.py)")
class RealModelTests(unittest.TestCase):
    def setUp(self):
        from sentinel.identity.faces import FaceAnalyzer

        self.analyzer = FaceAnalyzer(FACES / "face_detection_yunet_2023mar.onnx", FACES / "face_recognition_sface_2021dec.onnx")

    def test_blank_pictures_have_no_face_and_cannot_enroll(self):
        import numpy as np

        image = np.full((240, 320, 3), 128, dtype=np.uint8)
        self.assertEqual(self.analyzer.person_face(image, SETTINGS)[0], "no_face")
        with self.assertRaises(EnrollmentError):
            self.analyzer.enrollment_template(image, SETTINGS)

    @unittest.skipUnless((MOT09 / "MOT16-09.mp4").is_file(), "public MOT16 footage not downloaded (scripts/fetch_bench_footage.py)")
    def test_same_pedestrian_scores_higher_than_different_pedestrians(self):
        """Labelled public footage (MOT16-09 ground-truth identities): faces of one pedestrian
        over time are more similar than faces of different pedestrians. A sanity check of
        the pipeline, not an accuracy figure (scripts/identity_eval.py measures it)."""
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        from identity_eval import collect_faces, pair_scores

        faces = collect_faces(self.analyzer, MOT09, max_frames=300, every=6)
        same, different = pair_scores(faces, min_gap_ms=1000)
        self.assertGreater(len(same), 10)
        self.assertGreater(len(different), 10)
        mean = lambda values: sum(values) / len(values)
        self.assertGreater(mean(same), mean(different) + 0.2)
        false_matches = sum(score >= SETTINGS.match_threshold for score in different) / len(different)
        self.assertLess(false_matches, 0.05)


if __name__ == "__main__":
    unittest.main()
