"""Flame Mode against ArduPilot SITL (real ArduCopter firmware): the existing
Sentinel drone, in its Sentinel Thermal configuration, with a simulated
burn-monitoring accessory.

Skipped unless SENTINEL_SITL=flame. Start SITL with the Thermal parameters and
the simulated SF45/B lidar first (PowerShell):

    $env:WSLENV='SITL_PARAMS/u:SITL_EXTRA_ARGS/u'
    $env:SITL_PARAMS='config/drone/sentinel-thermal-sitl.parm'; $env:SITL_EXTRA_ARGS='--serial5=sim:sf45b'
    wsl -d Ubuntu -- bash /mnt/c/Users/pinde/CameraAI/scripts/sitl/start_sitl.sh 3
    $env:SENTINEL_SITL='flame'; python -m unittest tests.test_sentinel_flame_sitl

One scenario, in order, as an operator would run it:
1. register the accessory on the existing drone (simulated kit on the SITL aircraft);
2. switch from the security to the agricultural configuration (and back);
3. load a field;
4. fly a watch flight (operator-approved, flown by Sentinel in SITL) and receive
   simulated thermal observations computed from the SITL aircraft's real pose;
5. record fire-monitoring events (heat outside the boundary);
6. handle an accessory fault, low battery (flight-controller RTL) and loss of the
   Hub link (flight-controller failsafe);
7. close the operation and produce the report.

The fire and the thermal camera are simulated; the aircraft, its failsafes,
fence and battery model are ArduCopter's own."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sitl"))

ENABLED = os.environ.get("SENTINEL_SITL") == "flame"
GCS = os.environ.get("SENTINEL_SITL_GCS", "tcp:127.0.0.1:5763")
HUB = os.environ.get("SENTINEL_SITL_HUB", "tcp:127.0.0.1:5762")
ORIGIN = (45.0, -75.0)
FENCE_MAP = [[-10, -60], [150, -60], [150, 80], [-10, 80]]
FIELD_MAP = [[40, -30], [120, -30], [120, 40], [40, 40]]


def _deps() -> bool:
    try:
        import pymavlink  # noqa: F401
    except ImportError:
        return False
    return True


def site_config(directory: str) -> dict:
    return {
        "site": {"id": "flame_sitl", "name": "Flame Mode SITL", "map": {"width_m": 140, "height_m": 120, "origin_geo": list(ORIGIN)}},
        "cameras": [], "rules": [], "detector": {"type": "scripted"},
        "devices": [
            {"id": "farm_drone", "category": "drone", "name": "Farm drone (SITL)", "profile": "ardupilot_sitl", "adapter": "sitl",
             "connection": "udpin:127.0.0.1:14551", "allow_flight_commands": True, "drone_model": "sentinel_thermal", "map": {"x": 6, "y": 4}},
            {"id": "burn_kit", "category": "accessory", "name": "Burn-monitoring kit (simulated)", "profile": "simulated_burn_monitor_kit",
             "simulated": True, "drone_id": "farm_drone", "serial": "SIM-0001"},
        ],
        "alerts": {"channels": []},
        "storage": {"dir": directory},
        "drone": {"enabled": True, "device": "farm_drone", "gcs_system_id": 245,
                  "geofence": {"map_polygon": FENCE_MAP, "min_altitude_m": 5, "max_altitude_m": 25},
                  "safety": {"inspection_altitude_m": 20, "hover_s": 10, "rth_battery_pct": 30, "land_battery_pct": 15, "request_ttl_s": 900}},
    }


@unittest.skipUnless(ENABLED and _deps(), "set SENTINEL_SITL=flame with SITL running the Thermal parameters (see the module docstring)")
class FlameSitlScenario(unittest.TestCase):
    def setUp(self):
        from gcs_standin import SitlGroundStation

        from sentinel.companion.relay import MavlinkRelay
        from sentinel.drone import Geofence
        from sentinel.drone.base import map_point_to_geo
        from sentinel.drone.mission import plan_route, to_qgc_plan
        from sentinel.service import SentinelService

        self.gcs = SitlGroundStation(GCS)
        fence = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in FENCE_MAP], 25, 5)
        home = map_point_to_geo(ORIGIN, 6, 4)
        # Commissioning: the engineer loads Sentinel's fence into the flight controller.
        self.gcs.load_fence(to_qgc_plan(plan_route(home, map_point_to_geo(ORIGIN, 120, 5), fence, safe_transit_altitude_m=20,
                                                   inspection_altitude_m=20, hover_s=10), fence))
        self.gcs.set_param("BATT_CAPACITY", 3300)
        self.gcs.swap_battery()
        self.relay = MavlinkRelay(HUB, "udp:127.0.0.1:14551", policy="sitl").start()
        self.directory = tempfile.TemporaryDirectory()
        self.service = SentinelService(site_config(self.directory.name), start_cameras=False)
        self.notifications = []
        self.service.runtime.dispatcher.submit = self.notifications.append
        self.flame = self.service.flame
        self.flame.settings["watch_hover_s"] = 25.0
        self.drone = self.service.drone
        self.drone.video_status = lambda: {"state": "idle", "detail": "camera stream configured (SITL test)"}

    def tearDown(self):
        try:
            self.service.stop()
            for manager in self.service.drones.values():
                manager.adapter.disconnect()
        finally:
            self.relay.stop()
            self.gcs.set_param("BATT_CAPACITY", 3300)
            self.gcs.close()
            time.sleep(0.2)
            self.directory.cleanup()

    # ---- helpers --------------------------------------------------------------------

    def tick(self):
        self.drone.step(0.5)
        self.flame.step(0.5 * 3)  # SITL runs at speed-up 3: the simulated fire keeps pace with the aircraft
        time.sleep(0.5)

    def wait(self, predicate, timeout, what):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.tick()
            if predicate():
                return
        self.fail(f"timed out waiting for: {what} (blockers {self.drone.readiness()['blockers']}, state {self.drone.flight_state}, "
                  f"safety {self.drone.safety_state})")

    def landed(self):
        self.wait(lambda: self.drone.flight_state == "on_ground" and self.drone._flight is None, 240, "landed and disarmed")

    def kinds(self, oid):
        return [o["kind"] for o in self.flame.operation_view(oid, observations=5000)["observations"]]

    def consumed_mah(self):
        remaining = self.drone.adapter.telemetry().battery_pct or 100.0
        return max(5.0, (100.0 - remaining) / 100.0 * 3300)

    # ---- the scenario -----------------------------------------------------------------

    def test_flame_mode_on_the_existing_drone(self):
        from sentinel.drone.base import FlightCommandRejected

        drone = self.drone
        drone.set_conditions("farmer", precipitation=False, visibility_ok=True, wind_mps=3, temperature_c=15, daylight=True)
        self.wait(lambda: drone.adapter.params_complete() and drone.adapter.verified_simulator(), 90, "SITL verified and parameters read")
        self.wait(lambda: drone.readiness()["ready"], 120, "Sentinel Thermal ready in its security configuration")
        audit = drone.parameter_audit()
        self.assertTrue(audit["ok"], audit["blocking"])

        # 1. The existing drone with a registered accessory.
        kit = self.flame.accessory("burn_kit")
        self.assertEqual((kit["attachment"], kit["drone_model"], kit["compatibility"]["verdict"]), ("attached", "sentinel_thermal", "needs_validation"))
        self.assertEqual(self.flame.configuration("farm_drone"), "security")

        # 2. Security -> agricultural monitoring (after commissioning and inspection).
        self.flame.add_accessory_record("burn_kit", {"kind": "commissioning", "measured_mass_g": 262, "cg_checked": True, "hover_throttle_pct": 38}, "engineer")
        self.flame.add_accessory_record("burn_kit", {"kind": "inspection", "note": "pre-season"}, "engineer")
        self.flame.set_configuration("farm_drone", "agricultural_monitoring", "farmer")
        with self.assertRaises(FlightCommandRejected):
            drone.request_inspection(requested_by="sentinel", map_point=(6, 70), label="Yard", source="incident")
        self.assertLess(drone.model["flight"]["endurance_min_est"], 22.0)

        # 3. Field boundaries.
        field = self.flame.save_field(None, {"name": "Test strip", "boundary_map": FIELD_MAP, "watch_buffer_m": 20, "fireguard_m": 4,
                                             "crop": "barley stubble", "sensitive": [{"kind": "road", "label": "Concession road", "point_map": [145, 70]}]}, "farmer")
        self.assertEqual(field["geofence"]["state"], "pass")
        oid = self.flame.create_operation(field["id"], "farmer")["operation_id"]
        self.assertTrue(self.flame.operation_view(oid)["simulated"])
        today = date.today().isoformat()
        self.flame.add_authorization({"kind": "daily_authorization", "reference": "TEST-1", "valid_from": today, "valid_until": today}, "farmer")
        self.flame.add_qualification({"kind": "rpas_advanced", "person": "Sam Pilot", "expires": "2999-01-01"}, "farmer")
        self.flame.update_operation(oid, {"crew": [{"name": "Pat", "role": "burn_boss"}, {"name": "Sam Pilot", "role": "drone_pilot"},
                                                   {"name": "Lee", "role": "holding"}], "prescription": {"max_wind_mps": 7}}, "farmer")
        self.flame.add_weather(oid, {"wind_mps": 3, "wind_from_deg": 270, "humidity_pct": 45, "temperature_c": 15}, "farmer")
        self.flame.start_monitoring(oid, "farmer", acknowledge_incomplete=True)

        # 4-5. Watch flight over the east edge, flown by Sentinel in SITL after the operator approves it.
        request = self.flame.request_watch(oid, "east", "farmer")
        approved = drone.approve(request["request_id"], "Sam Pilot")
        self.assertEqual((approved.status, approved.mode), ("in_progress", "command"))
        self.wait(lambda: approved.phase == "inspecting", 180, "on station over the east edge")
        self.flame.report_ignition(oid, {"side": "west"}, "Pat")
        self.flame.simulate(oid, {"action": "spot_fire", "side": "east", "distance_m": 15}, "tester")
        self.wait(lambda: "heat_outside_boundary" in self.kinds(oid), 40, "heat outside the boundary observed from the SITL pose")
        outside = next(o for o in self.flame.operation_view(oid, observations=5000)["observations"] if o["kind"] == "heat_outside_boundary")
        self.assertEqual(outside["side"], "east")
        self.assertLess(outside["hotspot"]["uncertainty_m"], 15)
        self.assertTrue(any("outside" in n.title for n in self.notifications))

        # 6a. Equipment fault in flight: reported to people; the flight controller is unaffected.
        self.flame.simulate_accessory("burn_kit", "latch_open", "tester")
        self.tick()
        self.assertIn("accessory_fault", self.kinds(oid))
        self.assertIn("accessory_fault", drone.readiness()["blockers"])
        self.assertTrue(drone.adapter.telemetry().in_air)
        self.flame.simulate_accessory("burn_kit", "clear", "tester")

        # 6b. Low battery: ArduCopter's own battery model and failsafe return the aircraft.
        consumed = self.consumed_mah()
        self.gcs.set_param("BATT_CAPACITY", max(1.0, consumed / 0.75))
        self.wait(lambda: drone.safety_state == "low_battery_return", 40, "low battery detected")
        self.wait(lambda: drone.adapter.telemetry().flight_mode in ("RTL", "LAND"), 20, "returning home")
        self.landed()
        self.gcs.set_param("BATT_CAPACITY", 3300)
        safety = [o.get("safety_state") for o in self.flame.operation_view(oid, observations=5000)["observations"] if o["kind"] == "drone_safety"]
        self.assertIn("low_battery_return", safety)

        # 6c. Hub link loss on a second watch flight: the flight controller's GCS failsafe returns it.
        self.gcs.swap_battery()
        self.flame.add_accessory_record("burn_kit", {"kind": "inspection", "note": "latch re-seated after the fault"}, "engineer")
        self.wait(lambda: drone.readiness()["ready"], 90, "ready for the second flight")
        second = drone.approve(self.flame.request_watch(oid, "east", "farmer")["request_id"], "Sam Pilot")
        self.wait(lambda: second.phase == "enroute", 90, "en route")
        self.relay.set_hub_link(False)
        self.wait(lambda: drone.flight_state == "link_lost", 20, "link loss detected")
        time.sleep(4)  # FS_GCS_TIMEOUT is 5 s of simulated time
        self.relay.set_hub_link(True)
        self.wait(lambda: drone.adapter.telemetry().flight_mode in ("RTL", "LAND") or not drone.adapter.telemetry().in_air, 40,
                  "flight-controller failsafe")
        self.landed()
        safety = [o.get("safety_state") for o in self.flame.operation_view(oid, observations=5000)["observations"] if o["kind"] == "drone_safety"]
        self.assertIn("link_lost", safety)

        # 7. Post-burn, close, report; back to the security configuration.
        self.flame.start_post_burn(oid, "farmer")
        self.flame.close_operation(oid, {"result": "heat_remains", "note": "SITL scenario"}, "farmer")
        report = self.flame.report(oid)
        self.assertGreaterEqual(len(report["drone"]["flights"]), 2)
        kinds = {event["kind"] for event in report["events"]}
        self.assertTrue({"heat_outside_boundary", "accessory_fault", "drone_safety", "monitoring_started", "closed"} <= kinds, kinds)
        self.assertGreaterEqual(report["thermal"]["heat_outside_boundary"], 1)
        self.assertEqual(len(report["fingerprint"]), 64)
        from sentinel.flame.operations import forbidden_claims
        from sentinel.flame.reports import render_html

        self.assertEqual(forbidden_claims(render_html(report)), [])
        self.flame.set_configuration("farm_drone", "security", "farmer")
        self.assertEqual(drone.request_inspection(requested_by="farmer", map_point=(6, 70), label="Yard").status, "pending_approval")
        out = Path("bench_runs/flame_sitl")
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.html").write_text(render_html(report), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
