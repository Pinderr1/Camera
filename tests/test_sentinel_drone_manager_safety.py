"""The drone manager's safety monitor and records under faults: stale and
implausible telemetry, navigation loss, the altitude fence and the flight
controller's own fence verdict, honest advisories, refused safety commands,
database outages, expired approvals, lost contact, restart recovery, and the
setup journey. Offline (fake aircraft); scripts/sitl/fault_scenarios.py runs
the same faults against ArduPilot SITL."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import sentinel.drone.manager as manager_module
from sentinel.drone import DroneManager, FlightCommandRejected, Geofence
from sentinel.drone.base import DroneAdapter, DroneTelemetry, map_point_to_geo
from sentinel.drone.manager import DroneSafetyConfig
from sentinel.store import EventStore

ORIGIN = (45.0, -75.0)
FENCE = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 28], [0, 28]]], 25, 5)
HOME = map_point_to_geo(ORIGIN, 6, 4)
SHED = map_point_to_geo(ORIGIN, 35.5, 3)
GOOD_PARAMS = {"FS_GCS_ENABLE": 1, "FS_THR_ENABLE": 1, "BATT_FS_LOW_ACT": 2, "BATT_FS_CRT_ACT": 1, "FS_EKF_ACTION": 1, "FENCE_ENABLE": 1,
               "FENCE_TYPE": 5, "FENCE_ACTION": 1, "FENCE_ALT_MAX": 25, "RTL_ALT": 1500, "ARMING_CHECK": 1, "BATT_LOW_VOLT": 10.5,
               "BATT_CRT_VOLT": 10.0, "SIM_SPEEDUP": 1}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now


class Aircraft(DroneAdapter):
    """A pilot-flown ArduPilot aircraft whose telemetry the test writes; every timestamp follows the clock."""

    kind = "mavlink"
    flight_controller = "ardupilot"

    def __init__(self, clock, *, physical=True, commands=False):
        self.clock = clock
        self._physical = physical
        self.allow = commands
        self.t = DroneTelemetry(connected=True, link_ok=True, flight_mode="LOITER", battery_pct=95, battery_voltage=12.4, lat=HOME[0], lon=HOME[1],
                                gps_fix=3, satellites=14, hdop=0.8, ekf_ok=True, prearm_ok=True, home_lat=HOME[0], home_lon=HOME[1],
                                landed_state="on_ground")
        self.fresh = {"position", "gps", "battery", "ekf", "heartbeat"}
        self.params = dict(GOOD_PARAMS)
        self.fence = [list(p) for p in FENCE.polygon]
        self.commands = []
        self.refuse = False

    @property
    def is_physical(self):  # type: ignore[override]
        return self._physical

    def connect(self): pass
    def disconnect(self): pass

    def telemetry(self):
        now = int(self.clock.now * 1000)
        stamps = {"position": "telemetry_ms", "gps": "gps_ms", "battery": "battery_ms", "ekf": "ekf_ms", "heartbeat": "last_heartbeat_ms"}
        for signal, field in stamps.items():
            if signal in self.fresh:
                setattr(self.t, field, now)
        return DroneTelemetry(**self.t.as_dict())

    def parameters(self): return dict(self.params)
    def fence_polygon(self): return self.fence
    def commands_allowed(self): return (self.allow, "test simulator" if self.allow else "physical aircraft")

    def _command(self, name):
        self.commands.append(name)
        if not self.allow or self.refuse:
            raise FlightCommandRejected("refused")

    def takeoff(self, altitude_m): self._command("takeoff")
    def goto(self, lat, lon, altitude_m): self._command("goto")
    def hold(self): self._command("hold")
    def return_to_home(self): self._command("return_to_home")
    def land(self): self._command("land")

    def fly(self, lat, lon, alt, *, armed=True, mode="AUTO"):
        self.t.lat, self.t.lon, self.t.alt_m, self.t.armed, self.t.flight_mode = lat, lon, alt, armed, mode
        self.t.in_air = alt > 0.5
        self.t.landed_state = "in_air" if alt > 0.5 else "on_ground"


class SafetyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.real_time = manager_module.time
        self.clock = Clock()
        manager_module.time = self.clock
        self.directory = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.directory.name) / "sentinel.db")
        self.aircraft = Aircraft(self.clock)
        self.audit = []

    def tearDown(self):
        manager_module.time = self.real_time
        try:
            self.store.close()
        except Exception:
            pass
        self.directory.cleanup()

    def manager(self, **safety):
        settings = {"inspection_altitude_m": 12, "hover_s": 5} | safety
        manager = DroneManager(self.aircraft, FENCE, ORIGIN, DroneSafetyConfig(**settings), drone_id="d1", home=HOME, records=self.store,
                               audit=lambda actor, action, body: self.audit.append((action, body)))
        manager.video_status = lambda: {"state": "ready", "detail": "live picture verified"}
        return manager

    def run_steps(self, manager, seconds, dt=0.5):
        for _ in range(int(seconds / dt)):
            self.clock.now += dt
            manager.step(dt)

    def blockers(self, manager):
        return manager.readiness()["blockers"]

    # ---- readiness from telemetry Sentinel can vouch for ------------------------------

    def test_stale_reports_block_approval_even_when_their_last_values_look_fine(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        self.assertTrue(manager.readiness()["ready"], self.blockers(manager))
        self.aircraft.telemetry()
        self.aircraft.fresh -= {"gps", "battery"}  # GPS_RAW_INT and SYS_STATUS stop arriving; the last fix and % stay "good"
        self.clock.now += 20
        blockers = self.blockers(manager)
        self.assertIn("gps_reports_stale", blockers)
        self.assertIn("battery_reading_stale", blockers)
        self.aircraft.fresh |= {"gps", "battery"}
        self.assertTrue(manager.readiness()["ready"])
        self.aircraft.fresh.discard("position")
        self.clock.now += 10
        self.assertIn("position_not_current", self.blockers(manager))

    def test_the_flight_controllers_battery_verdict_blocks_although_the_percentage_is_high(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        self.aircraft.t.battery_state, self.aircraft.t.battery_voltage = "low", 10.3
        self.assertIn("battery_failsafe", self.blockers(manager))

    def test_camera_required_model_is_not_approved_without_a_live_picture(self):
        from sentinel.drone import catalog

        manager = self.manager()
        manager.model = catalog.MODEL_INDEX["sentinel_lite"]
        manager.video_status = lambda: {"state": "unreachable", "detail": "cannot open the stream"}
        self.assertIn("drone_video_unavailable", self.blockers(manager))
        manager.video_status = lambda: {"state": "unverified", "detail": "not checked yet"}
        self.assertNotIn("drone_video_unavailable", self.blockers(manager))

    # ---- in flight ------------------------------------------------------------------

    def test_gps_loss_in_flight_never_says_returning_home(self):
        manager = self.manager()
        self.aircraft.fly(*SHED, 12, mode="AUTO")
        self.run_steps(manager, 1)
        self.aircraft.t.gps_fix, self.aircraft.t.ekf_ok = 1, False
        self.aircraft.fly(*SHED, 12, mode="LAND")  # ArduPilot's EKF failsafe lands
        self.aircraft.t.gps_fix, self.aircraft.t.ekf_ok = 1, False
        self.aircraft.t.battery_pct = 25  # and the battery is low too: still no "returning home"
        self.run_steps(manager, 2)
        self.assertEqual(manager.safety_state, "navigation_lost")
        texts = [a["text"] for a in manager.advisories]
        self.assertTrue(any("Navigation failed" in t for t in texts), texts)
        self.assertFalse(any("returning home" in t for t in texts), texts)
        self.assertTrue(any("landing" in t for t in texts), texts)
        self.assertEqual(self.aircraft.commands, [], "a pilot-flown aircraft is never commanded")

    def test_altitude_ceiling_and_the_flight_controllers_fence_verdict(self):
        manager = self.manager()
        self.aircraft.fly(*SHED, 31, mode="GUIDED")  # inside the polygon, above the 25 m ceiling
        self.run_steps(manager, 1)
        self.assertEqual(manager.safety_state, "geofence_breach", "the ceiling is part of the geofence")
        self.aircraft.fly(*SHED, 12, mode="RTL")
        self.aircraft.t.fence_breach = "boundary"  # the FC saw a breach Sentinel's sample missed
        self.run_steps(manager, 1)
        self.assertEqual(manager.safety_state, "geofence_breach")
        self.assertIn("returning home", manager.advisories[-1]["text"])

    def test_stale_battery_is_not_used_to_command_or_advise(self):
        manager = self.manager()
        self.aircraft.fly(*SHED, 12)
        self.aircraft.t.battery_pct = 20
        self.aircraft.telemetry()
        self.aircraft.fresh.discard("battery")
        self.clock.now += 30
        self.run_steps(manager, 1)
        self.assertNotIn(manager.safety_state, ("low_battery_return", "critical_battery_land"))
        self.assertIn("battery_stale", {c["id"] for c in manager.health.conditions})

    def test_refused_safety_commands_are_rate_limited_and_notes_capped(self):
        self.aircraft = Aircraft(self.clock, physical=False, commands=True)
        manager = self.manager()
        self.aircraft.refuse = True
        self.aircraft.fly(*SHED, 12, mode="GUIDED")
        self.aircraft.t.battery_pct = 25
        self.run_steps(manager, 60)
        self.assertLessEqual(self.aircraft.commands.count("return_to_home"), 7, "at most one try every 10 s")
        self.assertLessEqual(len(manager.safety_notes), 20)

    def test_without_navigation_a_simulator_is_landed_not_sent_home(self):
        self.aircraft = Aircraft(self.clock, physical=False, commands=True)
        manager = self.manager()
        self.aircraft.fly(*SHED, 12, mode="GUIDED")
        self.aircraft.t.gps_fix, self.aircraft.t.battery_pct = 0, 25
        self.run_steps(manager, 1)
        self.assertIn("land", self.aircraft.commands)
        self.assertNotIn("return_to_home", self.aircraft.commands)

    # ---- records, approvals, lost contact, restarts ------------------------------------

    def test_a_database_outage_never_stops_the_safety_monitor(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1], label="Shed")
        manager.approve(request.request_id, "op")
        self.aircraft.fly(HOME[0], HOME[1], 12)
        self.run_steps(manager, 1)
        self.store.close()  # the database goes away mid-flight
        self.aircraft.fly(*SHED, 12)
        self.run_steps(manager, 3)
        self.assertEqual(request.phase, "inspecting", "the flight is still followed")
        self.aircraft.t.battery_pct = 20
        self.run_steps(manager, 1)
        self.assertEqual(manager.safety_state, "low_battery_return")
        self.assertGreater(manager.persist_failures, 0)
        checks = {c["id"]: c for c in manager.readiness()["checks"]}
        self.assertEqual(checks["records"]["state"], "warn")
        self.assertIn("may be incomplete", checks["records"]["detail"])
        manager.status()  # the dashboard still gets a status

    def test_records_left_unwritten_by_a_transient_outage_are_written_once_the_database_works(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1], label="Shed")
        manager.approve(request.request_id, "op")
        self.aircraft.fly(HOME[0], HOME[1], 12)
        self.run_steps(manager, 1)
        working = self.store._db

        class Locked:  # every statement fails, as with a locked or full database
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *args):
                raise sqlite3.OperationalError("database is locked")

        self.store._db = Locked()
        self.aircraft.fly(*SHED, 12)
        self.run_steps(manager, 20)
        self.aircraft.fly(HOME[0], HOME[1], 0, armed=False)
        self.run_steps(manager, 10)
        self.assertEqual(request.phase, "done")
        detail = next(c["detail"] for c in manager.readiness()["checks"] if c["id"] == "records")
        self.assertIn("waiting to be written", detail)
        self.store._db = working
        self.run_steps(manager, 10)
        stored = next(row for row in self.store.list_drone_inspections(drone_id="d1", limit=10) if row["request_id"] == request.request_id)
        self.assertEqual((stored["status"], stored["phase"]), (request.status, "done"), "no stale in-progress record is left behind")
        self.assertTrue(all(flight.get("ended_ms") for flight in self.store.list_drone_flights(drone_id="d1", limit=10)), "the flight log is closed")
        detail = next(c["detail"] for c in manager.readiness()["checks"] if c["id"] == "records")
        self.assertNotIn("waiting", detail)
        self.assertIn("may be incomplete", detail, "lost audit entries are still disclosed")

    def test_an_expired_request_cannot_be_approved_between_safety_ticks(self):
        manager = self.manager(request_ttl_s=60)
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        self.clock.now += 61  # no step() ran
        with self.assertRaises(FlightCommandRejected) as raised:
            manager.approve(request.request_id, "op")
        self.assertIn("expired", str(raised.exception))

    def test_an_inspection_whose_aircraft_is_never_heard_again_is_closed(self):
        manager = self.manager(lost_contact_s=600)
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        manager.approve(request.request_id, "op")
        self.aircraft.fly(HOME[0], HOME[1], 12)
        self.run_steps(manager, 1)
        self.aircraft.telemetry()
        self.aircraft.fresh.clear()
        self.aircraft.t.link_ok = False
        self.run_steps(manager, 300, dt=5)
        self.assertEqual(request.status, "in_progress", "still within any plausible flight")
        self.run_steps(manager, 400, dt=5)
        self.assertEqual(request.status, "interrupted")
        self.assertIn("outcome of this flight is unknown", request.note)
        self.assertIsNone(manager._mission, "later inspections are no longer blocked")

    def test_a_simulator_left_hovering_by_a_restart_is_sent_home_once(self):
        self.aircraft = Aircraft(self.clock, physical=False, commands=True)
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        manager.approve(request.request_id, "op")
        self.assertEqual(request.mode, "command")
        self.aircraft.fly(*SHED, 12, mode="GUIDED")
        self.run_steps(manager, 1)
        restarted = self.manager()  # Sentinel restarts; the aircraft holds over the target in GUIDED
        self.assertEqual(restarted.requests[request.request_id].status, "interrupted")
        self.aircraft.commands.clear()
        self.run_steps(restarted, 2)
        self.assertEqual(self.aircraft.commands, ["return_to_home"])
        self.assertIn("restart_recovery", [body.get("reason") for action, body in self.audit if action == "drone_auto_return_home"])

    def test_physical_aircraft_are_never_commanded_after_a_restart(self):
        manager = self.manager()
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1])
        manager.approve(request.request_id, "op")
        self.aircraft.fly(*SHED, 12, mode="GUIDED")
        self.run_steps(manager, 1)
        restarted = self.manager()
        self.run_steps(restarted, 2)
        self.assertEqual(self.aircraft.commands, [])

    def fly_to_target(self, manager):
        manager.set_conditions("op", precipitation=False, visibility_ok=True)
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1], label="Shed")
        manager.approve(request.request_id, "op")
        self.aircraft.fly(HOME[0], HOME[1], 12)
        self.run_steps(manager, 1)
        self.aircraft.fly(*SHED, 12)
        self.run_steps(manager, 2)
        self.assertEqual(request.phase, "inspecting")
        return request

    def test_landing_at_the_inspection_point_is_not_a_completed_inspection(self):
        manager = self.manager()
        request = self.fly_to_target(manager)
        self.aircraft.fly(*SHED, 0, armed=False, mode="LAND")  # e.g. the EKF failsafe landed it where it was
        self.run_steps(manager, 1)
        self.assertEqual(request.status, "aborted")
        self.assertIn("landed at the inspection point", request.note)

    def test_an_inspection_that_lands_away_from_home_is_not_completed(self):
        manager = self.manager()
        request = self.fly_to_target(manager)
        midway = map_point_to_geo(ORIGIN, 20, 4)
        self.aircraft.fly(*midway, 12, mode="RTL")
        self.run_steps(manager, 1)
        self.aircraft.fly(*midway, 0, armed=False, mode="LAND")  # a critical battery landed it on the way home
        self.run_steps(manager, 1)
        self.assertEqual(request.status, "aborted")
        self.assertIn("m from home", request.note)

    def test_battery_failsafe_near_home_is_aborted_without_commanding_the_aircraft(self):
        manager = self.manager()
        request = self.fly_to_target(manager)
        self.aircraft.fly(*HOME, 8, mode="RTL")
        self.run_steps(manager, 1)
        self.aircraft.t.battery_state = "critical"
        self.aircraft.fly(*HOME, 5, mode="LAND")
        self.run_steps(manager, 1)
        self.aircraft.fly(*HOME, 0, armed=False, mode="LAND")
        self.run_steps(manager, 1)
        self.assertEqual(request.status, "aborted")
        self.assertIn("battery_failsafe", request.note)
        self.assertEqual(self.aircraft.commands, [])

    def test_maintenance_verdicts_and_the_journey(self):
        manager = self.manager()
        self.assertEqual(manager.status()["journey"]["level"], "partially_configured", "no command lockout yet")
        self.aircraft.params |= {"MAV_OPTIONS": 1, "MAV_GCS_SYSID": 255}
        with self.assertRaises(ValueError):
            manager.add_maintenance("bench_test", "tech")
        manager.add_maintenance("commissioning", "engineer")
        manager.add_maintenance("equipment_check", "installer")
        record = manager.add_maintenance("bench_test", "tech", "props off", verdict="pass")
        self.assertEqual(record["fingerprint"], manager.parameter_audit()["fingerprint"])
        self.assertEqual(len(manager.maintenance()), 3, "records made in the same millisecond are all kept")
        journey = manager.status()["journey"]
        self.assertEqual(journey["level"], "bench_verified")
        self.aircraft.params["FENCE_MARGIN"] = 3  # a setting changed after the bench test
        self.assertEqual(manager.status()["journey"]["level"], "configured")


if __name__ == "__main__":
    unittest.main()
