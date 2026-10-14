"""Integration tests against a running ArduPilot SITL (real ArduCopter firmware).

Skipped unless SENTINEL_SITL=1. Start the simulator first (Linux or WSL2):

    bash scripts/sitl/start_sitl.sh 3        # speed-up 3
    set SENTINEL_SITL=1                       # PowerShell: $env:SENTINEL_SITL='1'
    python -m unittest tests.test_sentinel_drone_sitl

Sentinel uses SITL SERIAL1 (tcp 5762); the ground-station stand-in uses
SERIAL2 (tcp 5763). Tests run in order and leave the vehicle landed."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sitl"))

ENABLED = os.environ.get("SENTINEL_SITL") == "1"
HUB = os.environ.get("SENTINEL_SITL_HUB", "tcp:127.0.0.1:5762")
GCS = os.environ.get("SENTINEL_SITL_GCS", "tcp:127.0.0.1:5763")
ORIGIN = (45.0, -75.0)


def _deps() -> bool:
    try:
        import pymavlink  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(ENABLED and _deps(), "set SENTINEL_SITL=1 with ArduPilot SITL running (scripts/sitl/start_sitl.sh) and pymavlink installed")
class SitlIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gcs_standin import SitlGroundStation

        from sentinel.drone import Geofence
        from sentinel.drone.base import map_point_to_geo
        from sentinel.drone.mission import plan_route, to_qgc_plan

        cls.fence = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 28], [0, 28]]], 25, 5)
        cls.home = map_point_to_geo(ORIGIN, 6, 4)
        cls.gcs = SitlGroundStation(GCS)
        # Commissioning: the engineer loads the fence from a Sentinel export.
        route = plan_route(cls.home, map_point_to_geo(ORIGIN, 35.5, 3), cls.fence, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=4)
        cls.gcs.load_fence(to_qgc_plan(route, cls.fence))
        cls.gcs.set_param("BATT_CAPACITY", 3300)

    @classmethod
    def tearDownClass(cls):
        cls.gcs.close()

    def setUp(self):
        self.cleanup = []

    def tearDown(self):
        for call in reversed(self.cleanup):
            call()

    # ---- helpers --------------------------------------------------------------

    def manager(self, adapter, **safety):
        from sentinel.drone.manager import DroneManager, DroneSafetyConfig
        from sentinel.store import EventStore

        self.gcs.swap_battery()  # manual charging between flights
        adapter.connect()
        self.cleanup.append(adapter.disconnect)
        self.store = EventStore(Path(tempfile.mkdtemp()) / "sentinel.db")
        self.cleanup.append(self.store.close)
        settings = {"inspection_altitude_m": 12, "hover_s": 4} | safety
        manager = DroneManager(adapter, self.fence, ORIGIN, DroneSafetyConfig(**settings), records=self.store, drone_id="sitl", home=self.home,
                               audit=self.store.audit)
        manager.set_conditions("tester", precipitation=False, visibility_ok=True, wind_mps=2, temperature_c=12)
        self.wait(manager, lambda: manager.readiness()["ready"], 120, "aircraft ready")
        return manager

    def wait(self, manager, predicate, timeout, what):
        deadline = time.time() + timeout
        while time.time() < deadline:
            manager.step(0.5)
            if predicate():
                return
            time.sleep(0.5)
        self.fail(f"timed out waiting for: {what} (blockers {manager.readiness()['blockers']}, state {manager.flight_state})")

    def landed(self, manager):
        self.wait(manager, lambda: manager.flight_state in ("on_ground",) and manager._flight is None, 180, "landed and disarmed")

    def relay(self, policy):
        from sentinel.companion.relay import MavlinkRelay

        relay = MavlinkRelay(HUB, "udp:127.0.0.1:14551", policy=policy).start()
        self.cleanup.append(relay.stop)
        return relay

    # ---- tests ----------------------------------------------------------------

    def test_1_telemetry_parameters_and_audit(self):
        from sentinel.drone.base import FlightCommandRejected
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter

        manager = self.manager(MavlinkTelemetryAdapter(HUB))
        telemetry = manager.adapter.telemetry()
        self.assertTrue(telemetry.link_ok)
        self.assertGreaterEqual(telemetry.gps_fix, 3)
        self.assertTrue(telemetry.ekf_ok)
        self.assertEqual(telemetry.landed_state, "on_ground")
        self.assertAlmostEqual(telemetry.home_lat, self.home[0], places=5)
        self.assertTrue(telemetry.autopilot_version.startswith("4."))
        self.assertTrue(telemetry.sitl)
        audit = manager.parameter_audit()
        self.assertTrue(audit["ok"], [f for f in audit["findings"] if not f["ok"]])
        self.assertEqual(len(manager.adapter.fence_polygon()), 4)
        self.assertEqual(manager.mode()[0], "propose")
        with self.assertRaises(FlightCommandRejected):
            manager.adapter.takeoff(10)

    def test_2_companion_relay_drops_hub_flight_commands(self):
        from sentinel.drone.base import FlightCommandRejected
        from sentinel.drone.mavlink import SitlMavlinkAdapter

        relay = self.relay("telemetry")
        # The Hub-side adapter would allow commands (verified simulator, loopback), but
        # the companion's telemetry policy is a second, independent layer.
        manager = self.manager(SitlMavlinkAdapter("udpin:127.0.0.1:14551", allow_flight_commands=True))
        self.assertTrue(manager.adapter.commands_allowed()[0])
        with self.assertRaises(FlightCommandRejected):
            manager.adapter.takeoff(10)
        self.assertGreater(relay.status()["hub_frames_dropped"], 0)
        self.assertFalse(manager.adapter.telemetry().armed)

    def test_3_proposed_inspection_flown_by_the_pilot(self):
        from gcs_standin import SitlGroundStation  # noqa: F401
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter

        self.relay("telemetry")
        manager = self.manager(MavlinkTelemetryAdapter("udpin:127.0.0.1:14551"))
        request = manager.request_inspection(requested_by="sentinel", map_point=(35.5, 3), label="Shed", source="incident", incident_id="inc_1")
        manager.approve(request.request_id, "operator")
        self.assertEqual((request.status, request.mode), ("approved", "propose"))
        _, plan = manager.mission_export(request.request_id)
        self.gcs.load_mission(json.loads(plan))
        self.gcs.start_mission()
        self.wait(manager, lambda: request.phase == "inspecting", 120, "arrival at the inspection point")
        self.wait(manager, lambda: request.status == "completed", 180, "inspection completed")
        self.landed(manager)
        flights = self.store.list_drone_flights()
        self.assertEqual(len(flights), 1)
        self.assertGreater(flights[0]["max_alt_m"], 10)
        self.assertEqual(flights[0]["request_id"], request.request_id)
        stored = self.store.list_drone_inspections(incident_id="inc_1")[0]
        self.assertEqual(stored["status"], "completed")

    def test_4_sentinel_commanded_inspection_in_sitl(self):
        from sentinel.drone.mavlink import SitlMavlinkAdapter

        manager = self.manager(SitlMavlinkAdapter(HUB, allow_flight_commands=True))
        self.assertEqual(manager.mode()[0], "command")
        request = manager.request_inspection(requested_by="operator", map_point=(30, 10), label="Backyard")
        manager.approve(request.request_id, "operator")
        self.assertEqual(request.status, "in_progress")
        self.wait(manager, lambda: request.phase == "inspecting", 120, "arrival")
        self.wait(manager, lambda: request.status == "completed", 180, "completed")
        self.landed(manager)

    def test_5_link_loss_triggers_the_flight_controller_failsafe(self):
        from sentinel.drone.base import FlightCommandRejected
        from sentinel.drone.mavlink import SitlMavlinkAdapter

        relay = self.relay("sitl")
        manager = self.manager(SitlMavlinkAdapter("udpin:127.0.0.1:14551", allow_flight_commands=True))
        request = manager.request_inspection(requested_by="operator", map_point=(35.5, 3), label="Shed")
        manager.approve(request.request_id, "operator")
        self.wait(manager, lambda: request.phase == "enroute", 90, "en route")
        relay.set_hub_link(False)
        self.wait(manager, lambda: manager.flight_state == "link_lost", 20, "link loss detected")
        self.assertEqual(manager.safety_state, "link_lost")
        with self.assertRaises(FlightCommandRejected):
            manager.adapter.hold()
        time.sleep(4)  # FS_GCS_TIMEOUT is 5 s of simulated time
        relay.set_hub_link(True)
        self.wait(manager, lambda: manager.adapter.telemetry().flight_mode in ("RTL", "LAND") or not manager.adapter.telemetry().in_air,
                  30, "flight controller failsafe")
        self.landed(manager)
        self.assertIn(request.status, ("aborted", "completed"))
        audit = [row["action"] for row in self.store.list_audit(200)]
        self.assertIn("drone_link_lost", audit)

    def test_6_low_battery_return(self):
        from sentinel.drone.mavlink import SitlMavlinkAdapter

        manager = self.manager(SitlMavlinkAdapter(HUB, allow_flight_commands=True))
        request = manager.request_inspection(requested_by="operator", map_point=(35.5, 3), label="Shed")
        manager.approve(request.request_id, "operator")
        self.wait(manager, lambda: request.phase == "enroute", 90, "en route")
        try:
            # Simulator fault injection: a smaller declared capacity makes the remaining charge drop to ~25%.
            consumed = self._consumed_mah(manager)
            self.gcs.set_param("BATT_CAPACITY", max(1.0, consumed / 0.75))
            self.wait(manager, lambda: manager.safety_state == "low_battery_return", 30,
                      f"low battery detected (consumed {consumed:.0f} mAh, battery {manager.adapter.telemetry().battery_pct}%)")
            self.wait(manager, lambda: manager.adapter.telemetry().flight_mode in ("RTL", "LAND"), 15, "return home")
            self.landed(manager)
        finally:
            self.gcs.set_param("BATT_CAPACITY", 3300)
        self.assertEqual(request.status, "aborted")
        self.assertIn("drone_auto_return_home", [row["action"] for row in self.store.list_audit(200)])

    @staticmethod
    def _consumed_mah(manager) -> float:
        """From Sentinel's own reading: remaining % of the 3300 mAh declared capacity."""
        remaining = manager.adapter.telemetry().battery_pct or 100.0
        return max(5.0, (100.0 - remaining) / 100.0 * 3300)

if __name__ == "__main__":
    unittest.main()
