import unittest

from senior_safety.alerts import build_alert_payload
from senior_safety.clock import make_tick, synthesize_ticks
from senior_safety.schemas import NormalizedEvent
from senior_safety.state_machine import NightSafetyStateMachine, load_rules


RULES = load_rules("config/monitoring-rules.example.json")


def event(
    timestamp_ms,
    event_name,
    value=True,
    sensor_id="sensor",
    sensor_type="test",
    zone_id="route_zone",
    event_time_local="",
    confidence=1.0,
):
    return NormalizedEvent(
        event_id=f"{event_name}_{timestamp_ms}",
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        room="bedroom",
        zone_id=zone_id,
        timestamp_ms=timestamp_ms,
        event_name=event_name,
        value=value,
        event_time_local=event_time_local,
        confidence=confidence,
    )


class NightSafetyStateMachineTests(unittest.TestCase):
    def test_normal_bathroom_trip_does_not_alert(self):
        machine = NightSafetyStateMachine(RULES)
        decisions = [
            machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone")),
            machine.process(event(10_000, "route_motion", True, "route_01", "mmwave_presence")),
            machine.process(event(60_000, "bathroom_occupied", True, "bath_01", "mmwave_presence", "bathroom_private_zone")),
            machine.process(event(180_000, "bathroom_occupied", False, "bath_01", "mmwave_presence", "bathroom_private_zone")),
            machine.process(event(240_000, "bed_occupied", True, "bed_01", "bed_pressure", "bed_zone")),
        ]

        self.assertEqual(decisions[-1].state, "returned_to_bed")
        self.assertTrue(all(decision.severity != "urgent" for decision in decisions))

    def test_bathroom_overstay_becomes_low_alert(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        machine.process(event(60_000, "bathroom_occupied", True, "bath_01", "mmwave_presence", "bathroom_private_zone"))
        decision = machine.process(event(1_261_000, "bathroom_occupied", True, "bath_01", "mmwave_presence", "bathroom_private_zone"))

        self.assertEqual(decision.state, "bathroom_overstay")
        self.assertEqual(decision.severity, "low")
        self.assertIn("bathroom_overstay", decision.reason_codes)

    def test_fall_with_stillness_escalates_to_urgent(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        machine.process(event(5_000, "fall_suspected", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(6_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(10_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))
        decision = machine.process(event(56_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")
        self.assertIn("no_motion_45s", decision.reason_codes)

    def test_bed_zone_fall_signals_are_suppressed(self):
        machine = NightSafetyStateMachine(RULES)
        suspected = machine.process(event(5_000, "fall_suspected", True, "pose_01", "camera_pose", "bed_zone"))
        floor = machine.process(event(6_000, "floor_level_posture", True, "pose_01", "camera_pose", "bed_zone"))

        self.assertEqual(suspected.state, "asleep_in_bed")
        self.assertEqual(floor.state, "asleep_in_bed")
        self.assertEqual(suspected.severity, "none")
        self.assertEqual(floor.severity, "none")
        self.assertIn("bed_zone_fall_suppressed", suspected.suppressions)
        self.assertIn("bed_zone_fall_suppressed", floor.suppressions)

    def test_sofa_zone_suppresses_fall_and_no_motion_escalation(self):
        machine = NightSafetyStateMachine(RULES)
        suspected = machine.process(event(5_000, "fall_suspected", True, "pose_01", "camera_pose", "sofa_zone"))
        floor = machine.process(event(6_000, "floor_level_posture", True, "pose_01", "camera_pose", "sofa_zone"))
        still = machine.process(event(60_000, "no_motion", True, "pose_01", "camera_pose", "sofa_zone"))
        later = machine.process(make_tick(120_000))

        self.assertTrue(all(decision.severity == "none" for decision in (suspected, floor, still, later)))
        self.assertTrue(all(decision.state == "asleep_in_bed" for decision in (suspected, floor, still, later)))
        self.assertIn("fall_zone_suppressed", still.suppressions)

    def test_entering_safe_rest_clears_prior_floor_timers_before_escalation(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(6_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))
        decision = machine.process(event(60_000, "floor_level_posture", False, "pose_01", "camera_pose", "sofa_zone"))

        self.assertNotEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "none")
        self.assertIsNone(machine.floor_level_since_ms)
        self.assertIsNone(machine.no_motion_since_ms)

    def test_carpet_zone_does_not_suppress_fall_escalation(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(6_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))
        decision = machine.process(make_tick(52_000))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")

    def test_unknown_zone_does_not_urgent_alert_from_lying_alone(self):
        machine = NightSafetyStateMachine(RULES)
        floor = machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", ""))
        still = machine.process(event(6_000, "no_motion", True, "pose_01", "camera_pose", ""))
        later = machine.process(make_tick(120_000))

        self.assertTrue(all(decision.severity != "urgent" for decision in (floor, still, later)))
        self.assertIn("unknown_zone_lying_only", floor.suppressions)

    def test_confirmed_rapid_drop_on_carpet_triggers_after_floor_persistence(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(5_000, "fall_suspected", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(6_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        decision = machine.process(make_tick(9_000))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")
        self.assertIn("rapid_drop", decision.reason_codes)

    def test_low_full_body_confidence_does_not_open_fall_incident(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        decision = machine.process(
            event(5_000, "fall_suspected", True, "pose_01", "camera_pose", "carpet_zone", confidence=0.35)
        )

        self.assertNotIn(decision.state, {"possible_fall", "fallen_no_motion", "urgent_alert"})
        self.assertEqual(decision.severity, "none")
        self.assertIn("low_full_body_pose_confidence", decision.suppressions)

    def test_uncorroborated_fall_suspected_expires(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        suspected = machine.process(event(5_000, "fall_suspected", True, "pose_01", "camera_pose", "carpet_zone"))
        expired = machine.process(make_tick(20_000))

        self.assertEqual(suspected.state, "possible_fall")
        self.assertEqual(expired.state, "out_of_bed_unknown")
        self.assertNotEqual(expired.severity, "urgent")

    def test_floor_level_alone_does_not_escalate_to_urgent(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        fallen = machine.process(make_tick(10_000))
        urgent = machine.process(make_tick(66_000))

        self.assertEqual(fallen.state, "fallen_no_motion")
        self.assertEqual(urgent.state, "fallen_no_motion")
        self.assertEqual(urgent.severity, "low")

    def test_motion_on_floor_does_not_cancel_fall_incident(self):
        # A person struggling on the floor produces motion; that must not
        # de-escalate the incident while they remain floor-level.
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(10_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))
        moving = machine.process(event(20_000, "no_motion", False, "pose_01", "camera_pose", "carpet_zone"))
        later = machine.process(make_tick(66_000))

        self.assertIn(moving.state, {"possible_fall", "fallen_no_motion"})
        self.assertEqual(later.state, "fallen_no_motion")
        self.assertEqual(later.severity, "low")

    def test_bed_exit_no_return_escalates(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        low = machine.process(event(901_000, "route_motion", False, "route_01", "mmwave_presence"))
        urgent = machine.process(event(1_801_000, "route_motion", False, "route_01", "mmwave_presence"))

        self.assertEqual(low.state, "needs_check")
        self.assertEqual(low.severity, "low")
        self.assertEqual(urgent.state, "urgent_alert")
        self.assertEqual(urgent.severity, "urgent")

    def test_manual_help_always_urgent(self):
        machine = NightSafetyStateMachine(RULES)
        decision = machine.process(event(0, "manual_help_pressed", True, "help_01", "button", "bed_zone"))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")
        self.assertIn("manual_help_pressed", decision.reason_codes)

    def test_sensor_offline_state(self):
        machine = NightSafetyStateMachine(RULES)
        decision = machine.process(event(0, "sensor_offline", True, "bed_01", "bed_pressure", "bed_zone"))

        self.assertEqual(decision.state, "offline_or_blind")
        self.assertEqual(decision.severity, "low")
        self.assertIn("sensor_offline", decision.reason_codes)

    def test_ticks_drive_bed_exit_no_return_without_sensor_events(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        low = machine.process(make_tick(901_000))
        urgent = machine.process(make_tick(1_801_000))

        self.assertEqual(low.state, "needs_check")
        self.assertEqual(low.severity, "low")
        self.assertEqual(urgent.state, "urgent_alert")
        self.assertEqual(urgent.severity, "urgent")

    def test_ticks_drive_fall_persistence_to_urgent(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(6_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))
        decision = machine.process(make_tick(52_000))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")

    def test_repeat_low_alert_suppressed_by_cooldown(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        first = machine.process(make_tick(901_000))
        repeat = machine.process(make_tick(916_000))

        self.assertEqual(first.severity, "low")
        self.assertNotIn("alert_cooldown", first.suppressions)
        self.assertIsNotNone(build_alert_payload(first))
        self.assertEqual(repeat.severity, "low")
        self.assertIn("alert_cooldown", repeat.suppressions)
        self.assertIsNone(build_alert_payload(repeat))

    def test_repeat_urgent_fall_alert_suppressed_by_cooldown(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone"))
        machine.process(event(5_000, "floor_level_posture", True, "pose_01", "camera_pose", "carpet_zone"))
        machine.process(event(6_000, "no_motion", True, "pose_01", "camera_pose", "carpet_zone"))
        first = machine.process(make_tick(52_000))
        repeat = machine.process(make_tick(67_000))

        self.assertEqual(first.severity, "urgent")
        self.assertIsNotNone(build_alert_payload(first))
        self.assertEqual(repeat.severity, "urgent")
        self.assertIn("alert_cooldown", repeat.suppressions)
        self.assertIsNone(build_alert_payload(repeat))

    def test_daytime_bed_exit_does_not_alert(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone", event_time_local="2026-07-06T14:00:00"))
        decision = machine.process(make_tick(1_801_000))

        self.assertNotEqual(decision.severity, "urgent")
        self.assertNotIn("bed_exit_no_return", decision.reason_codes)

    def test_night_bed_exit_still_alerts_with_local_time(self):
        machine = NightSafetyStateMachine(RULES)
        machine.process(event(0, "bed_occupied", False, "bed_01", "bed_pressure", "bed_zone", event_time_local="2026-07-06T23:30:00"))
        decision = machine.process(make_tick(1_801_000))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")

    def test_manual_help_works_while_critical_sensor_stale(self):
        machine = NightSafetyStateMachine(RULES, critical_sensor_ids={"bed_01"})
        machine.process(event(0, "bed_occupied", True, "bed_01", "bed_pressure", "bed_zone"))
        stale = machine.process(make_tick(120_000))
        help_decision = machine.process(event(121_000, "manual_help_pressed", True, "help_01", "button", "bed_zone"))

        self.assertEqual(stale.state, "offline_or_blind")
        self.assertEqual(help_decision.state, "urgent_alert")
        self.assertEqual(help_decision.severity, "urgent")

    def test_synthesize_ticks_fills_gaps(self):
        events = [event(0, "bed_occupied", False), event(50_000, "route_motion", True)]
        combined = synthesize_ticks(events, 15_000)

        self.assertEqual([e.timestamp_ms for e in combined], [0, 15_000, 30_000, 45_000, 50_000])
        self.assertEqual([e.event_name for e in combined if e.event_name == "tick"], ["tick", "tick", "tick"])


if __name__ == "__main__":
    unittest.main()
