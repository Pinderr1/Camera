import unittest
from datetime import datetime

from sentinel.engine import TemporalEventEngine, in_active_hours

from sentinel_helpers import Driver, make_config, person

RESTRICTED = {"id": "shed_entry", "type": "zone_entry", "zone_types": ["restricted"], "severity": "high"}
YARD_DWELL = {"id": "yard_dwell", "type": "dwell", "zones": ["yard"], "min_dwell_s": 30, "severity": "low"}


class ZoneEntryTests(unittest.TestCase):
    def test_restricted_entry_fires_once_per_visit(self):
        driver = Driver(TemporalEventEngine(make_config([RESTRICTED])))
        driver.run(5, {"cam_a": [person(0.75, 0.8)]})
        entries = driver.of_type("zone_entry")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].severity, "high")
        self.assertEqual(entries[0].zone_id, "shed")
        self.assertIn("entered_restricted_zone", entries[0].reason_codes)

    def test_brief_flicker_into_zone_is_ignored(self):
        driver = Driver(TemporalEventEngine(make_config([RESTRICTED])))
        driver.run(2, {"cam_a": [person(0.45, 0.8)]})
        driver.run(0.4, {"cam_a": [person(0.52, 0.8)]})  # 400 ms across the boundary
        driver.run(3, {"cam_a": [person(0.45, 0.8)]})
        self.assertEqual(driver.of_type("zone_entry"), [])

    def test_activity_logged_once_per_subject_as_info(self):
        driver = Driver(TemporalEventEngine(make_config([])))
        driver.run(5, {"cam_a": [person(0.25, 0.8)]})
        activity = driver.of_type("activity")
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0].severity, "info")

    def test_rule_classes_filter(self):
        driver = Driver(TemporalEventEngine(make_config([RESTRICTED])))
        driver.run(5, {"cam_a": [person(0.75, 0.8, label="dog", size=(0.08, 0.08))]})
        self.assertEqual(driver.of_type("zone_entry"), [])


class DwellTests(unittest.TestCase):
    def test_dwell_fires_after_threshold(self):
        driver = Driver(TemporalEventEngine(make_config([YARD_DWELL])))
        driver.run(25, {"cam_a": [person(0.25, 0.8)]})
        self.assertEqual(driver.of_type("dwell"), [])
        driver.run(10, {"cam_a": [person(0.25, 0.8)]})
        dwell = driver.of_type("dwell")
        self.assertEqual(len(dwell), 1)
        self.assertGreaterEqual(dwell[0].details["dwell_s"], 30)

    def test_high_sensitivity_shortens_dwell(self):
        driver = Driver(TemporalEventEngine(make_config([YARD_DWELL]), sensitivity="high"))
        driver.run(25, {"cam_a": [person(0.25, 0.8)]})
        self.assertEqual(len(driver.of_type("dwell")), 1)

    def test_short_dropout_continues_the_same_visit(self):
        driver = Driver(TemporalEventEngine(make_config([YARD_DWELL, {**RESTRICTED, "zone_types": ["yard"], "id": "yard_entry", "severity": "low"}])))
        driver.run(20, {"cam_a": [person(0.25, 0.8)]})
        driver.run(3.4, {})  # detector misses the person: the track ends, the visit should not
        driver.run(15, {"cam_a": [person(0.26, 0.8)]})
        self.assertEqual(len(driver.of_type("zone_entry")), 1, "reacquisition must not count as a new entry")
        dwell = driver.of_type("dwell")
        self.assertEqual(len(dwell), 1)
        self.assertEqual(len({event.site_track_id for event in driver.events}), 1)

    def test_max_roam_separates_lingering_from_moving_around(self):
        rule = dict(YARD_DWELL, max_roam=2.0)
        small = (0.03, 0.08)  # a distant person: 8% of frame height

        walker = Driver(TemporalEventEngine(make_config([rule])))
        steps = iter(range(1000))
        walker.run(33, {"cam_a": lambda now: [person(0.02 + next(steps) * 0.003, 0.8, size=small)]})
        self.assertEqual(walker.of_type("dwell"), [], "slow transit is not lingering")

        circler = Driver(TemporalEventEngine(make_config([rule])))
        steps = iter(range(1000))

        def loop(now):
            # walks out and back along the yard every 20 s: ends where it started
            phase = (next(steps) % 100) / 100
            return [person(0.05 + 0.4 * (1 - abs(2 * phase - 1)), 0.8, size=small)]

        circler.run(40, {"cam_a": loop})
        self.assertEqual(circler.of_type("dwell"), [], "circling returns to the start but covers the zone")

        pacer = Driver(TemporalEventEngine(make_config([rule])))
        steps = iter(range(1000))
        pacer.run(40, {"cam_a": lambda now: [person(0.2 + 0.05 * ((next(steps) // 10) % 2), 0.8, size=small)]})
        dwell = pacer.of_type("dwell")
        self.assertEqual(len(dwell), 1)
        self.assertLessEqual(dwell[0].details["roam_body_heights"], 2.0)

    def test_stationary_vehicle_needs_arrival(self):
        rule = {"id": "vehicle_idle", "type": "dwell", "zones": ["yard"], "classes": ["vehicle"], "min_dwell_s": 20, "require_arrival": True, "severity": "medium"}

        def car(x):
            return [person(x, 0.8, label="car", size=(0.2, 0.15))]

        parked = Driver(TemporalEventEngine(make_config([rule])))
        parked.run(40, {"cam_a": car(0.25)})
        self.assertEqual(parked.of_type("dwell"), [], "a car parked since startup is not an arrival")

        arriving = Driver(TemporalEventEngine(make_config([rule])))
        positions = iter([0.05 + i * 0.015 for i in range(14)])
        arriving.run(3, {"cam_a": lambda now: car(next(positions, 0.25))})
        arriving.run(30, {"cam_a": car(0.25)})
        self.assertEqual(len(arriving.of_type("dwell")), 1)


class TemporalPatternTests(unittest.TestCase):
    def test_repeated_presence_counts_separate_visits(self):
        rule = {"id": "porch_repeat", "type": "repeated_presence", "zones": ["porch"], "count": 3, "window_s": 600, "severity": "medium"}
        driver = Driver(TemporalEventEngine(make_config([rule])))
        for _ in range(3):
            driver.run(5, {"cam_b": [person(0.5, 0.8)]})
            driver.run(15, {})
        repeated = driver.of_type("repeated_presence")
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0].details["visit_count"], 3)
        self.assertIn("same or different", repeated[0].details["identity_note"])

    def test_sequence_across_cameras(self):
        rule = {"id": "walk", "type": "sequence", "zones": ["yard", "porch"], "within_s": 60, "severity": "medium"}
        driver = Driver(TemporalEventEngine(make_config([rule])))
        driver.run(5, {"cam_a": [person(0.25, 0.8)]})
        driver.run(5, {})
        driver.run(5, {"cam_b": [person(0.5, 0.8)]})
        sequence = driver.of_type("sequence")
        self.assertEqual(len(sequence), 1)
        self.assertEqual(sequence[0].details["sequence"], ["yard", "porch"])

    def test_sequence_outside_time_window_is_ignored(self):
        rule = {"id": "walk", "type": "sequence", "zones": ["yard", "porch"], "within_s": 5, "severity": "medium"}
        driver = Driver(TemporalEventEngine(make_config([rule])))
        driver.run(5, {"cam_a": [person(0.25, 0.8)]})
        driver.run(8, {})
        driver.run(5, {"cam_b": [person(0.5, 0.8)]})
        self.assertEqual(driver.of_type("sequence"), [])

    def test_multi_camera_rule(self):
        rule = {"id": "traverse", "type": "multi_camera", "min_cameras": 2, "window_s": 60, "severity": "low"}
        driver = Driver(TemporalEventEngine(make_config([rule])))
        driver.run(5, {"cam_a": [person(0.25, 0.8)]})
        driver.run(5, {})
        driver.run(5, {"cam_b": [person(0.5, 0.8)]})
        events = driver.of_type("multi_camera")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["cameras"], ["cam_a", "cam_b"])


class GatingTests(unittest.TestCase):
    def test_active_hours_wrap_midnight(self):
        night = {"start": "22:00", "end": "06:00"}
        self.assertTrue(in_active_hours(int(datetime(2026, 1, 1, 23, 30).timestamp() * 1000), night))
        self.assertTrue(in_active_hours(int(datetime(2026, 1, 1, 2, 0).timestamp() * 1000), night))
        self.assertFalse(in_active_hours(int(datetime(2026, 1, 1, 12, 0).timestamp() * 1000), night))

    def test_rule_outside_active_hours_does_not_fire(self):
        rule = dict(RESTRICTED, active_hours={"start": "22:00", "end": "06:00"})
        driver = Driver(TemporalEventEngine(make_config([rule])))  # driver starts at noon
        driver.run(5, {"cam_a": [person(0.75, 0.8)]})
        self.assertEqual(driver.of_type("zone_entry"), [])

    def test_arm_mode_gating(self):
        rule = dict(RESTRICTED, arm_modes=["away"])
        home = Driver(TemporalEventEngine(make_config([rule]), arm_mode="home"))
        home.run(5, {"cam_a": [person(0.75, 0.8)]})
        self.assertEqual(home.of_type("zone_entry"), [])
        away = Driver(TemporalEventEngine(make_config([rule]), arm_mode="away"))
        away.run(5, {"cam_a": [person(0.75, 0.8)]})
        self.assertEqual(len(away.of_type("zone_entry")), 1)


if __name__ == "__main__":
    unittest.main()
