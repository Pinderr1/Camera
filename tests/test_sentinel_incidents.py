import tempfile
import time
import unittest
from pathlib import Path

from sentinel.incidents import AlertPolicy, AlertPolicyConfig, IncidentConfig, IncidentManager
from sentinel.models import SecurityEvent
from sentinel.store import EventStore


def event(n, severity="medium", event_type="zone_entry", site="st_1", camera="cam_a", t_ms=None, **details):
    t_ms = 1_000_000 + n * 1000 if t_ms is None else t_ms
    return SecurityEvent(
        event_id=f"evt_{n}",
        event_type=event_type,
        severity=severity,
        camera_id=camera,
        timestamp_ms=t_ms,
        started_ms=t_ms,
        confidence=0.8,
        summary=f"{event_type} {n}",
        site_track_id=site,
        details=details,
    )


class IncidentManagerTests(unittest.TestCase):
    def test_info_activity_never_opens_an_incident(self):
        manager = IncidentManager()
        incident, is_new = manager.add_event(event(1, severity="info", event_type="activity"))
        self.assertIsNone(incident)
        self.assertFalse(is_new)

    def test_events_from_one_subject_share_an_incident(self):
        manager = IncidentManager()
        first, new_first = manager.add_event(event(1))
        second, new_second = manager.add_event(event(2, event_type="dwell", camera="cam_b"))
        self.assertTrue(new_first)
        self.assertFalse(new_second)
        self.assertIs(first, second)
        self.assertEqual(first.cameras, ["cam_a", "cam_b"])

    def test_unrelated_subjects_on_other_cameras_get_separate_incidents(self):
        manager = IncidentManager()
        first, _ = manager.add_event(event(1))
        second, _ = manager.add_event(event(2, site="st_9", camera="cam_z"))
        self.assertIsNot(first, second)

    def test_corroborating_behaviours_raise_severity_one_level(self):
        manager = IncidentManager(IncidentConfig(corroboration_behaviors=3))
        manager.add_event(event(1, "low", "zone_entry"))
        manager.add_event(event(2, "low", "sequence"))
        incident, _ = manager.add_event(event(3, "low", "multi_camera"))
        self.assertEqual(incident.severity, "medium")

    def test_wildlife_and_security_on_one_camera_stay_separate(self):
        manager = IncidentManager()
        person, _ = manager.add_event(event(1, "medium", site="st_person"))
        bear, _ = manager.add_event(event(2, "medium", "wildlife_elevated_concern", site="st_bear", guardian=True))
        self.assertIsNot(person, bear)
        later, _ = manager.add_event(event(3, "medium", "dwell", site="st_other"))
        self.assertIs(later, person)  # security events still group by camera

    def test_one_animals_level_steps_are_not_corroboration(self):
        manager = IncidentManager(IncidentConfig(corroboration_behaviors=3))
        manager.add_event(event(1, "low", "wildlife_approaching", guardian=True))
        manager.add_event(event(2, "medium", "wildlife_elevated_concern", guardian=True))
        incident, _ = manager.add_event(event(3, "low", "zone_entry"))
        self.assertEqual(incident.severity, "medium")

    def test_title_follows_most_severe_behaviour(self):
        manager = IncidentManager()
        manager.add_event(event(1, "low"))
        incident, _ = manager.add_event(event(2, "high", "dwell"))
        manager.add_event(event(3, "low", "sequence"))
        self.assertEqual(incident.title, "dwell 2")

    def test_close_stale(self):
        manager = IncidentManager(IncidentConfig(close_after_s=60))
        incident, _ = manager.add_event(event(1))
        self.assertEqual(manager.close_stale(incident.updated_ms + 30_000), [])
        closed = manager.close_stale(incident.updated_ms + 61_000)
        self.assertEqual(closed[0].status, "closed")
        self.assertEqual(manager.open, {})


class AlertPolicyTests(unittest.TestCase):
    def setUp(self):
        self.manager = IncidentManager()
        self.policy = AlertPolicy(AlertPolicyConfig.from_dict({"notify_min_severity": {"home": "high", "away": "medium"}, "max_notifications": 2}))

    def test_threshold_depends_on_arm_mode(self):
        incident, _ = self.manager.add_event(event(1, "medium"))
        self.assertIsNone(self.policy.evaluate(incident, [], "home", 0))
        self.assertIsNone(self.policy.evaluate(incident, [], "disarmed", 0))
        notification = self.policy.evaluate(incident, [], "away", 0)
        self.assertEqual(notification.priority, 4)
        self.assertIn("not intent", notification.message)

    def test_notifies_again_only_when_severity_rises(self):
        incident, _ = self.manager.add_event(event(1, "medium"))
        self.assertIsNotNone(self.policy.evaluate(incident, [], "away", 0))
        self.assertIsNone(self.policy.evaluate(incident, [], "away", 1000))
        self.manager.add_event(event(2, "high"))
        self.assertEqual(self.policy.evaluate(incident, [], "away", 2000).severity, "high")

    def test_rate_limit_holds_back_and_reports(self):
        for n in range(3):
            incident, _ = self.manager.add_event(event(n, "high", site=f"st_{n}", camera=f"cam_{n}"))
            result = self.policy.evaluate(incident, [], "away", n * 1000)
            if n < 2:
                self.assertIsNotNone(result)
            else:
                self.assertIsNone(result)
        self.assertEqual(self.policy.suppressed, 1)
        incident, _ = self.manager.add_event(event(9, "high", site="st_9", camera="cam_9"))
        later = self.policy.evaluate(incident, [], "away", 700_000)
        self.assertIn("held back", later.message)

    def test_wildlife_urgent_review_is_not_held_back_by_the_rate_limit(self):
        for n in range(2):
            incident, _ = self.manager.add_event(event(n, "high", site=f"st_{n}", camera=f"cam_{n}"))
            self.assertIsNotNone(self.policy.evaluate(incident, [], "away", n * 1000))
        urgent = event(5, "high", "wildlife_urgent_review", site="st_bear", camera="cam_5", guardian=True, level="urgent_review", notify="always")
        incident, _ = self.manager.add_event(urgent)
        self.assertIsNotNone(self.policy.evaluate(incident, [urgent], "away", 3000))
        other, _ = self.manager.add_event(event(6, "high", site="st_6", camera="cam_6"))
        self.assertIsNone(self.policy.evaluate(other, [], "away", 4000))  # it still counted toward the limit

    def test_unacknowledged_high_incident_escalates_once(self):
        incident, _ = self.manager.add_event(event(1, "high"))
        self.policy.evaluate(incident, [], "home", incident.opened_ms)
        self.assertEqual(self.policy.due_escalations([incident], incident.opened_ms + 10_000), [])
        due = self.policy.due_escalations([incident], incident.opened_ms + 301_000)
        self.assertEqual(due[0].kind, "escalation")
        self.assertEqual(self.policy.due_escalations([incident], incident.opened_ms + 900_000), [])


class StoreTests(unittest.TestCase):
    def test_round_trip_filters_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "s.db")
            manager = IncidentManager()
            old = event(1, "low", t_ms=1_000)
            new = event(2, "high", t_ms=int(time.time() * 1000), site="st_2", camera="cam_b")
            for item in (old, new):
                incident, _ = manager.add_event(item)
                store.add_event(item)
                store.upsert_incident(incident)
            self.assertEqual([row["event_id"] for row in store.list_events(min_severity="medium")], ["evt_2"])
            self.assertEqual(len(store.list_events(camera_id="cam_a")), 1)
            self.assertEqual(len(store.list_incidents()), 2)
            store.audit("tester", "settings_changed", {"arm_mode": "away"})
            self.assertEqual(store.list_audit()[0]["actor"], "tester")

            media = Path(directory) / "media"
            media.mkdir()
            (media / "old.jpg").write_bytes(b"x")
            purged = store.purge(events_before_ms=10_000, media_before_ms=int(time.time() * 1000) + 60_000, media_root=media)
            self.assertEqual(purged["events"], 1)
            self.assertEqual(purged["media_files"], 1)
            self.assertEqual([row["event_id"] for row in store.list_events()], ["evt_2"])
            store.close()


if __name__ == "__main__":
    unittest.main()
