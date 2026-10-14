"""Drone V1 core: parameter audit, routes and exports, camera pose and drone
observations, and the manager's operating modes, readiness, logs and recovery.
No simulator needed (tests/test_sentinel_drone_sitl.py covers real SITL)."""

import json
import tempfile
import unittest
from pathlib import Path

import sentinel.drone.manager as manager_module
from sentinel.drone import DroneManager, FlightCommandRejected, Geofence
from sentinel.drone.ardupilot import audit_parameters, configuration_fingerprint, flight_state, is_sitl, version_text
from sentinel.drone.base import DroneAdapter, DroneTelemetry, distance_m, map_point_to_geo
from sentinel.drone.manager import DroneSafetyConfig
from sentinel.drone.mission import parse_qgc_plan, plan_route, to_qgc_plan, to_waypoints_text
from sentinel.drone.observation import CameraModel, CameraPose, InspectionObserver, ground_to_image, image_to_ground
from sentinel.incidents import DISCLAIMER
from sentinel.models import Detection
from sentinel.store import EventStore

ORIGIN = (45.0, -75.0)
FENCE = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 28], [0, 28]]], 25, 5)
HOME = map_point_to_geo(ORIGIN, 6, 4)
SHED = map_point_to_geo(ORIGIN, 35.5, 3)
GOOD_PARAMS = {"FS_GCS_ENABLE": 1, "FS_THR_ENABLE": 1, "BATT_FS_LOW_ACT": 2, "BATT_FS_CRT_ACT": 1, "FS_EKF_ACTION": 1, "FENCE_ENABLE": 1,
               "FENCE_TYPE": 5, "FENCE_ACTION": 1, "FENCE_ALT_MAX": 25, "RTL_ALT": 1500, "ARMING_CHECK": 1, "BATT_LOW_VOLT": 10.5,
               "BATT_CRT_VOLT": 10.0, "SIM_SPEEDUP": 1}


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now


class FakeArduPilot(DroneAdapter):
    """A pilot-flown ArduPilot aircraft whose telemetry the test writes."""

    kind = "mavlink"
    is_physical = True
    flight_controller = "ardupilot"

    def __init__(self):
        self.t = DroneTelemetry(connected=True, link_ok=True, flight_mode="LOITER", battery_pct=95, lat=HOME[0], lon=HOME[1], gps_fix=3,
                                satellites=14, hdop=0.8, ekf_ok=True, prearm_ok=True, home_lat=HOME[0], home_lon=HOME[1], landed_state="on_ground")
        self.params = dict(GOOD_PARAMS)
        self.fence = [list(p) for p in FENCE.polygon]
        self.commands = []

    def connect(self): pass
    def disconnect(self): pass
    def telemetry(self): return DroneTelemetry(**self.t.as_dict())
    def parameters(self): return dict(self.params)
    def fence_polygon(self): return self.fence
    def commands_allowed(self): return False, "physical aircraft"

    def _refuse(self, name):
        self.commands.append(name)
        raise FlightCommandRejected("telemetry only")

    def takeoff(self, altitude_m): self._refuse("takeoff")
    def goto(self, lat, lon, altitude_m): self._refuse("goto")
    def hold(self): self._refuse("hold")
    def return_to_home(self): self._refuse("return_to_home")
    def land(self): self._refuse("land")

    def fly(self, lat, lon, alt, *, armed=True, mode="AUTO"):
        self.t.lat, self.t.lon, self.t.alt_m, self.t.armed, self.t.flight_mode = lat, lon, alt, armed, mode
        self.t.in_air = alt > 0.5
        self.t.landed_state = "in_air" if alt > 0.5 else "on_ground"


class ArduPilotTests(unittest.TestCase):
    def test_audit_passes_good_settings_and_names_each_problem(self):
        self.assertTrue(audit_parameters(GOOD_PARAMS, max_altitude_m=25, safe_transit_altitude_m=12, fence_polygon=FENCE.polygon, geofence_polygon=FENCE.polygon)["ok"])
        bad = GOOD_PARAMS | {"FS_GCS_ENABLE": 0, "FENCE_ALT_MAX": 100, "RTL_ALT": 500, "FENCE_TYPE": 1}
        result = audit_parameters(bad, max_altitude_m=25, safe_transit_altitude_m=12)
        failing = {f["param"] for f in result["blocking"]}
        self.assertEqual(failing, {"FS_GCS_ENABLE", "FENCE_ALT_MAX", "RTL_ALT", "FENCE_TYPE"})

    def test_arducopter_47_names_are_understood(self):
        renamed = {k: v for k, v in GOOD_PARAMS.items() if k not in ("RTL_ALT", "ARMING_CHECK")} | {"RTL_ALT_M": 15, "ARMING_SKIPCHK": 0}
        result = audit_parameters(renamed, max_altitude_m=25, safe_transit_altitude_m=12)
        self.assertTrue(result["ok"], result["blocking"])
        rtl = next(f for f in result["findings"] if f["param"] == "RTL_ALT")
        self.assertIn("RTL_ALT_M=15", rtl["actual"])
        skipping = audit_parameters(renamed | {"ARMING_SKIPCHK": 4}, max_altitude_m=25, safe_transit_altitude_m=12)
        self.assertFalse(next(f for f in skipping["findings"] if f["param"] == "ARMING_CHECK")["ok"])

    def test_fence_polygon_must_lie_inside_the_geofence(self):
        loose = [list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [60, 0], [60, 28], [0, 28]]]
        none = audit_parameters(GOOD_PARAMS, max_altitude_m=25, safe_transit_altitude_m=12, fence_polygon=[], geofence_polygon=FENCE.polygon)
        outside = audit_parameters(GOOD_PARAMS, max_altitude_m=25, safe_transit_altitude_m=12, fence_polygon=loose, geofence_polygon=FENCE.polygon)
        self.assertEqual(next(f for f in none["findings"] if f["param"] == "fence polygon")["actual"], "none loaded")
        self.assertIn("2 vertex", next(f for f in outside["findings"] if f["param"] == "fence polygon")["actual"])

    def test_missing_parameters_block(self):
        self.assertFalse(audit_parameters({}, max_altitude_m=25, safe_transit_altitude_m=12)["ok"])

    def test_commissioning_changes_are_reported_but_learned_values_ignored(self):
        baseline = GOOD_PARAMS | {"MOT_THST_HOVER": 0.35, "INS_ACCOFFS_X": 0.01}
        drifted = baseline | {"MOT_THST_HOVER": 0.4, "INS_ACCOFFS_X": 0.02}
        self.assertEqual(configuration_fingerprint(baseline), configuration_fingerprint(drifted))
        changed = audit_parameters(drifted | {"FENCE_MARGIN": 5}, max_altitude_m=25, safe_transit_altitude_m=12, baseline=baseline)
        self.assertEqual(changed["changed_since_commissioning"], ["FENCE_MARGIN"])
        self.assertTrue(changed["ok"], "a change is a warning, not a blocker")

    def test_flight_state_and_helpers(self):
        t = DroneTelemetry(connected=True, link_ok=True)
        self.assertEqual(flight_state(t), "on_ground")
        t.armed = True
        self.assertEqual(flight_state(t), "armed")
        t.in_air, t.flight_mode = True, "RTL"
        self.assertEqual(flight_state(t), "returning")
        t.link_ok = False
        self.assertEqual(flight_state(t), "link_lost")
        self.assertEqual(version_text(67568383), "4.7.2")
        self.assertTrue(is_sitl({"SIM_SPEEDUP": 1}))
        self.assertFalse(is_sitl({"FS_GCS_ENABLE": 1}))


class MissionTests(unittest.TestCase):
    def test_route_climbs_over_home_and_never_descends_below_transit(self):
        route = plan_route(HOME, SHED, FENCE, safe_transit_altitude_m=15, inspection_altitude_m=12, hover_s=10, label="Shed")
        self.assertEqual([w["kind"] for w in route.waypoints], ["takeoff", "inspect", "return_home"])
        self.assertTrue(all(w["alt_m"] == 15 for w in route.waypoints))
        self.assertIn("raised", route.notes[0])
        self.assertEqual(route.problems, [])

    def test_route_outside_fence_or_through_an_exclusion_is_rejected(self):
        outside = plan_route(HOME, map_point_to_geo(ORIGIN, 60, 3), FENCE, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=5)
        self.assertIn("route_outside_geofence", outside.problems)
        excluded = Geofence(FENCE.polygon, 25, 5, exclusions=[[list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[18, 0], [22, 0], [22, 28], [18, 28]]]])
        crossing = plan_route(HOME, SHED, excluded, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=5)
        self.assertIn("route_inside_exclusion_0", crossing.problems)
        too_high = plan_route(HOME, SHED, FENCE, safe_transit_altitude_m=30, inspection_altitude_m=12, hover_s=5)
        self.assertIn("climb_above_max_altitude", too_high.problems)

    def test_exports_for_ground_stations(self):
        route = plan_route(HOME, SHED, FENCE, safe_transit_altitude_m=12, inspection_altitude_m=12, hover_s=8)
        plan = parse_qgc_plan(json.dumps(to_qgc_plan(route, FENCE, request_id="insp_1")))
        self.assertEqual([item["command"] for item in plan["mission"]["items"]], [22, 16, 20])
        self.assertEqual(plan["mission"]["items"][1]["params"][0], 8, "hold time at the inspection point")
        self.assertEqual(plan["geoFence"]["polygons"][0]["polygon"], FENCE.polygon)
        lines = to_waypoints_text(route).splitlines()
        self.assertEqual(lines[0], "QGC WPL 110")
        self.assertEqual(len(lines), 5)
        self.assertEqual([line.split("\t")[3] for line in lines[2:]], ["22", "16", "20"])


class ObservationTests(unittest.TestCase):
    camera = CameraModel(hfov_deg=80, vfov_deg=50)

    def pose(self, **changes):
        values = {"lat": SHED[0], "lon": SHED[1], "height_m": 12, "heading_deg": 30, "pitch_deg": -90}
        return CameraPose(**(values | changes))

    def test_straight_down_centre_is_below_the_aircraft(self):
        estimate = image_to_ground(self.pose(), self.camera, 0.5, 0.5)
        self.assertTrue(estimate.available)
        self.assertLess(distance_m(SHED, (estimate.lat, estimate.lon)), 0.05)
        self.assertIn("assumed from configuration", estimate.method)

    def test_projection_round_trip(self):
        for pose in (self.pose(), self.pose(pitch_deg=-50, heading_deg=200), self.pose(pitch_deg=-35, height_m=20, attitude_source="gimbal_reported")):
            target = map_point_to_geo(ORIGIN, 37, 6) if pose.pitch_deg == -90 else None
            if target is None:
                # a point the oblique camera actually sees: project the image centre first
                centre = image_to_ground(pose, self.camera, 0.5, 0.6)
                target = (centre.lat, centre.lon)
            u, v = ground_to_image(pose, self.camera, *target)
            back = image_to_ground(pose, self.camera, u, v)
            self.assertLess(distance_m(target, (back.lat, back.lon)), 0.05)

    def test_no_estimate_when_pose_cannot_support_one(self):
        self.assertIn("horizon", image_to_ground(self.pose(pitch_deg=-5), self.camera, 0.5, 0.5).reason)
        self.assertIn("old", image_to_ground(self.pose(age_s=3), self.camera, 0.5, 0.5).reason)
        self.assertIn("too low", image_to_ground(self.pose(height_m=1), self.camera, 0.5, 0.5).reason)
        reported = image_to_ground(self.pose(pitch_deg=-60, attitude_source="gimbal_reported"), self.camera, 0.5, 0.5)
        assumed = image_to_ground(self.pose(pitch_deg=-60), self.camera, 0.5, 0.5)
        self.assertLess(reported.uncertainty_m, assumed.uncertainty_m)

    def test_observer_tracks_frames_and_describes_behaviour_only(self):
        observer = InspectionObserver("insp_1", SHED, "Shed", self.camera, ORIGIN)
        person = Detection("person", 0.8, (0.45, 0.45, 0.06, 0.08))
        for frame in range(10):
            new = observer.update([person, Detection("person", 0.7, (0.1, 0.1, 0.05, 0.08))], 1000 + frame * 200, self.pose())
            if frame == 1:
                self.assertEqual(len(new), 2, "both confirmed on the second frame")
        summary = observer.summary()
        self.assertEqual(summary["max_visible"], {"person": 2})
        text = " ".join(summary["text"])
        self.assertIn("up to 2 visible at the same time near Shed", text)
        self.assertIn("may or may not be the same", text)
        self.assertTrue(text.endswith(DISCLAIMER))
        for word in ("intruder", "suspicious", "burglar"):
            self.assertNotIn(word, text.lower())
        nearest = min(summary["observations"], key=lambda o: o["position"]["distance_from_target_m"])
        self.assertLess(nearest["position"]["distance_from_target_m"], 2.0)

    def test_empty_inspection_does_not_claim_the_area_is_clear(self):
        observer = InspectionObserver("insp_2", SHED, "Shed", self.camera, ORIGIN)
        observer.update([], 1000, self.pose())
        self.assertIn("does not prove the area was empty", observer.summary()["text"][0])
        observer = InspectionObserver("insp_3", SHED, "Shed", self.camera, ORIGIN)
        for frame in range(4):
            observer.update([Detection("person", 0.8, (0.45, 0.45, 0.06, 0.08))], 1000 + frame * 200, None)
        self.assertIn("No ground position estimated (no camera position)", " ".join(observer.summary()["text"]))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.real_time = manager_module.time
        self.clock = FakeClock()
        manager_module.time = self.clock
        self.directory = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.directory.name) / "sentinel.db")
        self.adapter = FakeArduPilot()
        self.audit = []

    def tearDown(self):
        manager_module.time = self.real_time
        self.store.close()
        self.directory.cleanup()

    def manager(self, **safety):
        settings = {"inspection_altitude_m": 12, "hover_s": 5} | safety
        return DroneManager(self.adapter, FENCE, ORIGIN, DroneSafetyConfig(**settings), drone_id="d1", home=HOME, records=self.store,
                            audit=lambda actor, action, body: self.audit.append(action))

    def run_steps(self, manager, seconds, dt=0.5):
        for _ in range(int(seconds / dt)):
            self.clock.now += dt
            manager.step(dt)

    def test_real_aircraft_is_propose_only_and_readiness_names_blockers(self):
        manager = self.manager()
        self.assertEqual(manager.mode()[0], "propose")
        self.assertEqual(manager.readiness()["blockers"], ["conditions_not_confirmed"])
        manager.set_conditions("op", precipitation=True, visibility_ok=True)
        self.assertEqual(manager.readiness()["blockers"], ["conditions_outside_limits"])
        manager.set_conditions("op", precipitation=False, visibility_ok=True, wind_mps=12)
        self.assertEqual(manager.readiness()["blockers"], ["conditions_outside_limits"])
        manager.set_conditions("op", precipitation=False, visibility_ok=True, wind_mps=4, temperature_c=5)
        self.assertTrue(manager.readiness()["ready"])
        self.adapter.params["FENCE_ENABLE"] = 0
        self.assertEqual(manager.readiness()["blockers"], ["flight_controller_settings"])
        self.adapter.params["FENCE_ENABLE"] = 1
        self.adapter.t.prearm_ok = False
        self.adapter.t.messages = ["PreArm: Compass not calibrated"]
        check = next(c for c in manager.readiness()["checks"] if c["id"] == "prearm")
        self.assertEqual(check["detail"], "PreArm: Compass not calibrated")
        self.clock.now += 3700
        self.adapter.t.prearm_ok = True
        self.assertIn("conditions_not_confirmed", manager.readiness()["blockers"], "attestations expire")

    def test_pilot_flown_inspection_is_followed_from_telemetry(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="sentinel", lat=SHED[0], lon=SHED[1], label="Shed", source="incident",
                                             incident_id="inc_1", reason="Person entered Shed")
        manager.approve(request.request_id, "alice")
        self.assertEqual((request.status, request.phase, request.mode), ("approved", "awaiting_pilot", "propose"))
        self.assertEqual(self.adapter.commands, [], "Sentinel sent nothing to the aircraft")
        _, plan = manager.mission_export(request.request_id)
        self.assertEqual(json.loads(plan)["sentinel"]["approved_by"], "alice")
        self.adapter.fly(HOME[0], HOME[1], 0.2)  # pilot arms
        self.run_steps(manager, 1)
        self.assertEqual(request.status, "in_progress")
        self.adapter.fly(HOME[0], HOME[1], 12)
        self.run_steps(manager, 1)
        self.assertEqual(request.phase, "enroute")
        self.adapter.fly(*SHED, 12)
        self.run_steps(manager, 6)
        self.assertEqual(request.phase, "inspecting")
        self.adapter.fly(HOME[0], HOME[1], 12)
        self.run_steps(manager, 1)
        self.adapter.fly(HOME[0], HOME[1], 0, armed=False, mode="LAND")
        self.run_steps(manager, 1)
        self.assertEqual(request.status, "completed")
        flights = self.store.list_drone_flights(drone_id="d1")
        self.assertEqual(len(flights), 1)
        self.assertGreater(flights[0]["distance_m"], 55)
        self.assertEqual(flights[0]["request_id"], request.request_id)
        self.assertEqual(self.store.list_drone_inspections(incident_id="inc_1")[0]["status"], "completed")
        self.assertIn("drone_inspection_completed", self.audit)

    def test_low_battery_on_a_pilot_flown_aircraft_is_advice_not_a_command(self):
        manager = self.manager()
        self.adapter.fly(*SHED, 12)
        self.adapter.t.battery_pct = 25
        self.run_steps(manager, 1)
        self.assertEqual(manager.safety_state, "low_battery_return")
        self.assertEqual(self.adapter.commands, [])
        self.assertIn("return home now", manager.advisories[-1]["text"])

    def test_approval_window_expires_and_restart_never_resumes_a_flight(self):
        manager = self.manager(approval_window_s=60)
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        unflown = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        manager.approve(unflown.request_id, "op")
        self.run_steps(manager, 61)
        self.assertEqual(unflown.status, "not_flown")
        flying = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        manager.approve(flying.request_id, "op")
        self.adapter.fly(*SHED, 12)
        self.run_steps(manager, 1)
        self.assertEqual(flying.status, "in_progress")
        restarted = self.manager()  # Sentinel restarts mid-flight
        recovered = restarted.requests[flying.request_id]
        self.assertEqual(recovered.status, "interrupted")
        self.assertIsNone(restarted._mission)
        self.assertTrue(self.store.list_drone_flights(drone_id="d1")[0]["interrupted"])

    def test_second_approval_is_blocked_while_one_is_open(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        first = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        second = manager.request_inspection(requested_by="op", map_point=(30, 10))
        manager.approve(first.request_id, "op")
        with self.assertRaises(FlightCommandRejected) as raised:
            manager.approve(second.request_id, "op")
        self.assertIn("already_flying", str(raised.exception))

    def test_commissioning_records_a_reference_configuration(self):
        manager = self.manager()
        record = manager.add_maintenance("commissioning", "engineer", "initial setup")
        self.assertTrue(record["audit_ok"])
        self.assertNotIn("parameters", record)
        self.adapter.params["FENCE_MARGIN"] = 3
        audit = manager.parameter_audit()
        self.assertEqual(audit["changed_since_commissioning"], ["FENCE_MARGIN"])
        with self.assertRaises(ValueError):
            manager.add_maintenance("teleport", "x")

    def test_launch_point_warning(self):
        manager = self.manager()
        self.adapter.fly(*SHED, 0, armed=False)
        check = next(c for c in manager.readiness()["checks"] if c["id"] == "launch_point")
        self.assertEqual(check["state"], "warn")


if __name__ == "__main__":
    unittest.main()
