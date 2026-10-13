import unittest
from types import SimpleNamespace

import sentinel.drone.manager as manager_module
from sentinel.drone import DroneManager, FlightCommandRejected, Geofence
from sentinel.drone.base import DroneTelemetry, map_point_to_geo
from sentinel.drone.manager import DroneSafetyConfig
from sentinel.drone.mavlink import MavlinkTelemetryAdapter, apply_mavlink_message
from sentinel.drone.mock import SimulatedDrone

ORIGIN = (45.0, -75.0)
FENCE = Geofence(
    polygon=[list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 30], [0, 30]]],
    max_altitude_m=25,
    min_altitude_m=5,
)


def make_manager(**safety):
    drone = SimulatedDrone(map_point_to_geo(ORIGIN, 5, 5), cruise_speed_mps=10)
    drone.connect()
    audit = []
    manager = DroneManager(
        drone,
        FENCE,
        ORIGIN,
        DroneSafetyConfig(hover_s=2, **safety),
        audit=lambda actor, action, body: audit.append((actor, action)),
    )
    return manager, drone, audit


class FakeTime:
    """Stands in for the manager's wall clock so hover timers and request
    expiry advance with simulated steps instead of real seconds."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


def run(manager, seconds, dt=0.5):
    for _ in range(int(seconds / dt)):
        manager_module.time.now += dt
        manager.step(dt)


class GeofenceTests(unittest.TestCase):
    def test_inside_and_outside(self):
        inside = map_point_to_geo(ORIGIN, 20, 15)
        outside = map_point_to_geo(ORIGIN, 60, 15)
        self.assertEqual(FENCE.violations(*inside, 10), [])
        self.assertIn("outside_geofence", FENCE.violations(*outside, 10))
        self.assertIn("above_max_altitude", FENCE.violations(*inside, 40))


class DroneManagerTests(unittest.TestCase):
    def setUp(self):
        self.real_time = manager_module.time
        manager_module.time = FakeTime()

    def tearDown(self):
        manager_module.time = self.real_time

    def test_inspection_requires_approval_then_completes(self):
        manager, drone, audit = make_manager()
        request = manager.request_inspection(requested_by="sentinel", map_point=(30, 10), label="Shed", source="incident", incident_id="inc_1")
        run(manager, 5)
        self.assertEqual(drone.mode, "docked", "nothing flies before an operator approves")
        manager.approve(request.request_id, "alice")
        run(manager, 90)
        self.assertEqual(request.status, "completed")
        self.assertEqual(drone.mode, "docked")
        self.assertIn(("alice", "drone_inspection_approved"), audit)

    def test_duplicate_requests_for_one_incident_are_merged(self):
        manager, _, _ = make_manager()
        first = manager.request_inspection(requested_by="sentinel", map_point=(30, 10), incident_id="inc_1")
        second = manager.request_inspection(requested_by="sentinel", map_point=(31, 10), incident_id="inc_1")
        self.assertIs(first, second)

    def test_target_outside_geofence_is_rejected(self):
        manager, _, _ = make_manager()
        with self.assertRaises(FlightCommandRejected):
            manager.request_inspection(requested_by="op", map_point=(80, 10))
        manager.command("takeoff", "op", {"altitude_m": 10})
        with self.assertRaises(FlightCommandRejected):
            manager.command("goto", "op", {"lat": 46.0, "lon": -75.0})

    def test_preflight_blocks_low_battery(self):
        manager, drone, _ = make_manager()
        drone.battery = 40
        request = manager.request_inspection(requested_by="op", map_point=(20, 10))
        with self.assertRaises(FlightCommandRejected) as raised:
            manager.approve(request.request_id, "op")
        self.assertIn("battery_below_launch_minimum", str(raised.exception))
        self.assertEqual(request.status, "pending_approval")

    def test_denied_and_expired_requests(self):
        manager, _, _ = make_manager(request_ttl_s=10)
        denied = manager.request_inspection(requested_by="op", map_point=(20, 10))
        manager.deny(denied.request_id, "op", "neighbour's cat")
        stale = manager.request_inspection(requested_by="op", map_point=(21, 10))
        run(manager, 12)
        self.assertEqual(denied.status, "denied")
        self.assertEqual(stale.status, "expired")
        with self.assertRaises(FlightCommandRejected):
            manager.approve(stale.request_id, "op")

    def test_low_battery_forces_return_home(self):
        manager, drone, audit = make_manager()
        manager.command("takeoff", "op", {"altitude_m": 10})
        run(manager, 6)
        drone.battery = 25
        run(manager, 1)
        self.assertEqual(manager.safety_state, "low_battery_return")
        self.assertIn(drone.mode, {"returning", "landing"})
        self.assertIn(("system", "drone_auto_return_home"), audit)

    def test_link_loss_triggers_simulated_failsafe(self):
        manager, drone, _ = make_manager()
        manager.command("takeoff", "op", {"altitude_m": 10})
        run(manager, 6)
        manager.command("goto", "op", dict(zip(("lat", "lon"), map_point_to_geo(ORIGIN, 30, 20))) | {"altitude_m": 10})
        run(manager, 2)
        drone.set_link(False)
        run(manager, 1)
        self.assertEqual(manager.safety_state, "link_lost")
        run(manager, 6)
        self.assertEqual(drone.mode, "returning")
        with self.assertRaises(FlightCommandRejected):
            manager.command("hold", "op")


class MavlinkTests(unittest.TestCase):
    def test_commands_are_refused(self):
        adapter = MavlinkTelemetryAdapter("udp:127.0.0.1:14550")
        for call in (lambda: adapter.takeoff(10), lambda: adapter.goto(45, -75, 10), adapter.hold, adapter.return_to_home, adapter.land):
            with self.assertRaises(FlightCommandRejected):
                call()

    def test_telemetry_parsing(self):
        telemetry = DroneTelemetry()

        def message(kind, **fields):
            return SimpleNamespace(get_type=lambda: kind, **fields)

        apply_mavlink_message(telemetry, message("HEARTBEAT", base_mode=128 | 1, custom_mode=6), 1000)
        apply_mavlink_message(telemetry, message("SYS_STATUS", battery_remaining=72, voltage_battery=15800), 1000)
        apply_mavlink_message(telemetry, message("GLOBAL_POSITION_INT", lat=450000000, lon=-750000000, relative_alt=12000, hdg=9000, vx=300, vy=400), 1000)
        apply_mavlink_message(telemetry, message("GPS_RAW_INT", fix_type=3, satellites_visible=12), 1000)
        self.assertTrue(telemetry.armed)
        self.assertEqual(telemetry.battery_pct, 72)
        self.assertAlmostEqual(telemetry.battery_voltage, 15.8)
        self.assertEqual((telemetry.lat, telemetry.lon, telemetry.alt_m), (45.0, -75.0, 12.0))
        self.assertTrue(telemetry.in_air)
        self.assertEqual(telemetry.ground_speed_mps, 5.0)
        self.assertEqual(telemetry.gps_fix, 3)


if __name__ == "__main__":
    unittest.main()
