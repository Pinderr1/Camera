import unittest

from senior_safety.schemas import NormalizedEvent
from senior_safety.state_machine import NightSafetyStateMachine, load_rules


RULES = load_rules("config/monitoring-rules.example.json")


def event(timestamp_ms, event_name, value=True, sensor_id="sensor", sensor_type="test", zone_id="route_zone"):
    return NormalizedEvent(
        event_id=f"{event_name}_{timestamp_ms}",
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        room="bedroom",
        zone_id=zone_id,
        timestamp_ms=timestamp_ms,
        event_name=event_name,
        value=value,
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
        machine.process(event(5_000, "fall_suspected", True, "pose_01", "camera_pose"))
        machine.process(event(6_000, "floor_level_posture", True, "pose_01", "camera_pose"))
        machine.process(event(10_000, "no_motion", True, "pose_01", "camera_pose"))
        decision = machine.process(event(56_000, "no_motion", True, "pose_01", "camera_pose"))

        self.assertEqual(decision.state, "urgent_alert")
        self.assertEqual(decision.severity, "urgent")
        self.assertIn("no_motion_45s", decision.reason_codes)

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


if __name__ == "__main__":
    unittest.main()
