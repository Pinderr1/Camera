"""Sentinel Lite against ArduPilot SITL (real ArduCopter firmware) with the Lite's
commissioning parameters: no companion computer, the flight controller's own
command lockout, one shared radio link for the Hub and the pilot.

Skipped unless SENTINEL_SITL=lite. Start the simulator with the Lite parameters:

    SITL_PARAMS=config/drone/sentinel-lite-sitl.parm bash scripts/sitl/start_sitl.sh 3
    $env:SENTINEL_SITL='lite'; python -m unittest tests.test_sentinel_drone_lite_sitl   # PowerShell

The HM30 radio is emulated by Sentinel's relay in pass-through mode on SITL
SERIAL1 (tcp 5762): everything the flight controller sends reaches both the Hub
and the pilot's ground station (the stand-in), exactly as on one radio link.
Tests run in order and leave the vehicle landed."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sitl"))

ENABLED = os.environ.get("SENTINEL_SITL") == "lite"
FC = os.environ.get("SENTINEL_SITL_HUB", "tcp:127.0.0.1:5762")
HUB_UDP, PILOT_UDP = 14551, 14561
ORIGIN = (45.0, -75.0)
SHED = (35.5, 3.0)


def _deps() -> bool:
    try:
        import cv2  # noqa: F401
        import pymavlink  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(ENABLED and _deps(), "set SENTINEL_SITL=lite with SITL started from config/drone/sentinel-lite-sitl.parm (pymavlink, OpenCV)")
class LiteSitlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gcs_standin import SitlGroundStation

        from sentinel.companion.relay import MavlinkRelay
        from sentinel.drone import Geofence, catalog
        from sentinel.drone.base import map_point_to_geo
        from sentinel.drone.mission import plan_route, to_qgc_plan

        cls.model = catalog.MODEL_INDEX["sentinel_lite"]
        cls.fence = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 28], [0, 28]]], 25, 5)
        cls.home = map_point_to_geo(ORIGIN, 6, 4)
        # The HM30 link: one radio carrying the Hub's and the pilot's MAVLink.
        cls.radio = MavlinkRelay(FC, f"udp:127.0.0.1:{HUB_UDP}", [f"udp:127.0.0.1:{PILOT_UDP}"], policy="sitl").start()
        cls.pilot = SitlGroundStation(f"udpin:127.0.0.1:{PILOT_UDP}")
        if cls.pilot.get_param("MAV_OPTIONS") != 1 or cls.pilot.get_param("MAV_GCS_SYSID") != 255:
            cls.pilot.close()
            cls.radio.stop()
            raise unittest.SkipTest("SITL was not started with config/drone/sentinel-lite-sitl.parm (MAV_OPTIONS 1, MAV_GCS_SYSID 255)")
        cls.pilot.start_heartbeats()
        route = plan_route(cls.home, map_point_to_geo(ORIGIN, *SHED), cls.fence, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=4)
        cls.pilot.load_fence(to_qgc_plan(route, cls.fence))  # commissioning: the engineer loads Sentinel's fence
        cls.pilot.set_param("BATT_CAPACITY", 3300)
        cls.pilot.set_param("BATT_LOW_MAH", 0)

    @classmethod
    def tearDownClass(cls):
        cls.pilot.close()
        cls.radio.stop()

    def setUp(self):
        self.cleanup = []

    def tearDown(self):
        self.radio.set_hub_link(True)
        self.radio.set_gcs_link(True)
        for call in reversed(self.cleanup):
            call()

    # ---- helpers --------------------------------------------------------------

    def manager(self, adapter, *, video=None):
        from sentinel.drone import catalog
        from sentinel.drone.manager import DroneManager, DroneSafetyConfig, WeatherLimits
        from sentinel.store import EventStore

        self.pilot.swap_battery()  # manual charging between flights
        adapter.connect()
        self.cleanup.append(adapter.disconnect)
        self.directory = tempfile.mkdtemp()
        self.store = EventStore(Path(self.directory) / "sentinel.db")
        self.cleanup.append(self.store.close)
        manager = DroneManager(adapter, self.fence, ORIGIN, DroneSafetyConfig(inspection_altitude_m=12, hover_s=6), records=self.store,
                               drone_id="lite", home=self.home, audit=self.store.audit, model=self.model,
                               weather=WeatherLimits.from_dict(catalog.weather_defaults(self.model)))
        if video is None:
            manager.video_status = lambda: {"state": "idle", "detail": "camera stream configured"}
        else:
            from sentinel.drone.video import DroneVideoWorker

            settings = catalog.video_settings(self.model, {"video": video}, simulated=True) | {"fps": 5}
            worker = DroneVideoWorker(manager, settings, config={}, media_root=Path(self.directory) / "media", record_evidence=self.store.record_evidence)
            manager.video_status = worker.status
            worker.start()
            self.cleanup.append(lambda: (worker.stop(), worker.join(5)))
        manager.set_conditions("tester", precipitation=False, visibility_ok=True, wind_mps=2, temperature_c=12, daylight=True)
        self.wait(manager, lambda: manager.adapter.telemetry().link_ok, 30, "Hub hears the aircraft")
        # What the pilot's ground station does on connect; Sentinel overhears it on the shared radio.
        self.pilot.download_parameters()
        self.pilot.download_fence()
        self.wait(manager, lambda: manager.readiness()["ready"], 120, "Lite ready")
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
        self.wait(manager, lambda: manager.flight_state == "on_ground" and manager._flight is None, 180, "landed and disarmed")

    def fly_proposed(self, manager, label="Shed", incident_id=""):
        import json

        request = manager.request_inspection(requested_by="sentinel", map_point=SHED, label=label, source="incident" if incident_id else "operator",
                                             incident_id=incident_id, reason="Person entered Shed" if incident_id else "")
        self.assertEqual(request.status, "pending_approval")
        manager.approve(request.request_id, "operator")
        self.assertEqual((request.status, request.mode), ("approved", "propose"))
        _, plan = manager.mission_export(request.request_id)
        self.pilot.load_mission(json.loads(plan))
        self.pilot.start_mission()
        return request

    # ---- tests ----------------------------------------------------------------

    def test_1_flight_controller_lockout_and_passive_audit(self):
        from sentinel.drone.base import FlightCommandRejected
        from sentinel.drone.mavlink import SitlMavlinkAdapter

        # An adapter that *believes* it may command (verified simulator, loopback, allow_flight_commands):
        # the Lite's flight controller ignores it anyway, because the Hub is not the pilot's ground station.
        manager = self.manager(SitlMavlinkAdapter(f"udpin:127.0.0.1:{HUB_UDP}", allow_flight_commands=True, command_timeout_s=3))
        checks = {check["id"]: check for check in manager.readiness()["checks"]}
        self.assertEqual(checks["lockout"]["state"], "pass")
        self.assertIn("system id 255", checks["lockout"]["detail"])
        self.assertEqual(checks["companion"]["state"], "na")
        audit = manager.parameter_audit()
        self.assertTrue(audit["ok"], [f for f in audit["findings"] if not f["ok"]])
        self.assertEqual(len(manager.adapter.fence_polygon()), 4, "fence read from the pilot's download")
        self.assertIn("MNT1_TYPE", [f["param"] for f in audit["findings"] if not f["ok"] and not f["blocking"]])
        telemetry = manager.adapter.telemetry()
        self.assertGreaterEqual(telemetry.gps_fix, 3, "position streams come from the commissioned stream rates")
        self.assertTrue(telemetry.ekf_ok)
        self.assertTrue(manager.adapter.commands_allowed()[0])
        with self.assertRaises(FlightCommandRejected):
            manager.adapter.takeoff(10)
        time.sleep(2)
        self.assertFalse(manager.adapter.telemetry().armed, "the flight controller ignored the Hub's commands")

    def test_2_pilot_flown_lite_inspection_with_video_and_evidence(self):
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter

        video = {"source": {"type": "simulated_aerial", "objects": [
            {"label": "person", "path": [{"t": 0, "map": [34.6, 4.0]}, {"t": 20, "map": [35.4, 4.4]}, {"t": 40, "map": [34.6, 4.0]}]}]}}
        manager = self.manager(MavlinkTelemetryAdapter(f"udpin:127.0.0.1:{HUB_UDP}"), video=video)
        request = self.fly_proposed(manager, incident_id="inc_lite")
        self.assertEqual(request.aircraft, "Sentinel Lite (aircraft, telemetry only)")
        self.assertTrue(request.flight_estimate["ok"])
        self.wait(manager, lambda: request.phase == "inspecting", 120, "arrival at the inspection point")
        self.wait(manager, lambda: request.status == "completed", 180, "inspection completed")
        self.landed(manager)
        deadline = time.time() + 10
        while not request.observations.get("final") and time.time() < deadline:
            time.sleep(0.2)
        summary = request.observations
        self.assertEqual(summary["model"], "sentinel_lite")
        self.assertEqual(summary["max_visible"], {"person": 1})
        positioned = [row for row in summary["observations"] if row["position"]["available"]]
        self.assertTrue(positioned)
        self.assertLess(positioned[0]["position"]["distance_from_target_m"], 6.0)
        self.assertTrue(request.evidence_paths)
        self.assertIn(request.evidence_paths[0], self.store.evidence_hashes(request.evidence_paths))
        stored = self.store.list_drone_inspections(incident_id="inc_lite")[0]
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(len(self.store.list_drone_flights()), 1)

    def test_3_radio_loss_makes_the_flight_controller_return_home(self):
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter

        manager = self.manager(MavlinkTelemetryAdapter(f"udpin:127.0.0.1:{HUB_UDP}"))
        request = self.fly_proposed(manager)
        self.wait(manager, lambda: request.phase == "enroute", 90, "en route")
        self.radio.set_hub_link(False)
        self.radio.set_gcs_link(False)  # the one radio is gone: neither the Hub nor the pilot's ground station hears the aircraft
        self.wait(manager, lambda: manager.flight_state == "link_lost", 20, "link loss detected")
        time.sleep(4)  # FS_GCS_TIMEOUT is 5 s of simulated time (speed-up 3)
        self.radio.set_hub_link(True)
        self.radio.set_gcs_link(True)
        self.wait(manager, lambda: manager.adapter.telemetry().flight_mode in ("RTL", "LAND") or not manager.adapter.telemetry().in_air,
                  30, "flight controller failsafe")
        self.landed(manager)
        self.assertEqual(request.status, "aborted")
        actions = [row["action"] for row in self.store.list_audit(300)]
        self.assertIn("drone_link_lost", actions)
        self.assertNotIn("drone_auto_return_home", actions, "Sentinel never commands a pilot-flown aircraft; the FC acted")

    def test_4_low_battery_failsafe_and_operator_advisory(self):
        from sentinel.drone.mavlink import MavlinkTelemetryAdapter

        manager = self.manager(MavlinkTelemetryAdapter(f"udpin:127.0.0.1:{HUB_UDP}"))
        request = self.fly_proposed(manager)
        self.wait(manager, lambda: request.phase == "enroute", 90, "en route")
        try:
            remaining = manager.adapter.telemetry().battery_pct or 100.0
            consumed = max(5.0, (100.0 - remaining) / 100.0 * 3300)
            capacity = max(1.0, consumed / 0.75)  # simulator fault: the pack had far less charge than declared (~25% left)
            self.pilot.set_param("BATT_CAPACITY", capacity)
            self.pilot.set_param("BATT_LOW_MAH", capacity * 0.3)
            self.wait(manager, lambda: manager.adapter.telemetry().flight_mode in ("RTL", "LAND"), 30, "flight controller battery failsafe")
            self.wait(manager, lambda: any(a["reason"] == "low_battery" for a in manager.advisories), 15, "operator advisory")
            self.landed(manager)
        finally:
            self.pilot.set_param("BATT_CAPACITY", 3300)
            self.pilot.set_param("BATT_LOW_MAH", 0)
        self.assertEqual(request.status, "aborted")
        advisory = next(a for a in manager.advisories if a["reason"] == "low_battery")
        # The flight controller's own failsafe acted first, so Sentinel explains it rather than asking the pilot to act.
        self.assertIn(advisory["text"], ("Battery low: the aircraft is returning home", "Battery low: the pilot should return home now"))
        self.assertNotIn("drone_auto_return_home", [row["action"] for row in self.store.list_audit(300)])


if __name__ == "__main__":
    unittest.main()
