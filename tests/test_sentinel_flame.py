"""Flame Mode: removable drone accessories, the agricultural configuration,
fields, burn operations, simulated thermal watch, reports and the AI Guide.

Everything runs against the built-in simulator; nothing here is evidence about
real aircraft, real fires or real thermal cameras."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from sentinel.config import load_config
from sentinel.drone import accessories as acc
from sentinel.drone import catalog
from sentinel.drone.base import FlightCommandRejected, map_point_to_geo
from sentinel.flame import fields as fieldlib
from sentinel.flame import operations as ops
from sentinel.flame.thermal import SimulatedFireField

try:
    from fastapi.testclient import TestClient
except ImportError:  # API dependencies are optional for the core
    TestClient = None

FARM = "config/sentinel.farm.example.json"
FIELD_MAP = [[100, 60], [500, 60], [500, 300], [100, 300]]


def farm_config(directory: str) -> dict:
    config = load_config(FARM, resolve_secrets=False)
    config["storage"] = {"dir": directory}
    return config


class AccessoryCompatibilityTests(unittest.TestCase):
    def test_monitoring_kit_fits_thermal_on_paper_with_measurements_outstanding(self):
        result = acc.evaluate(acc.PROFILE_INDEX["sentinel_burn_monitor_kit"], catalog.MODEL_INDEX["sentinel_thermal"])
        self.assertEqual(result["verdict"], "needs_validation")
        states = {check["id"]: check["state"] for check in result["checks"]}
        self.assertEqual(states["mass"], "pass")
        self.assertEqual(states["sensors"], "pass")
        for item in ("balance", "heat", "power", "data"):
            self.assertEqual(states[item], "unverified", item)
        self.assertEqual(result["mass"]["takeoff_g"], 4150)
        self.assertEqual(result["mass"]["limit_g"], 6300)

    def test_kit_needs_the_thermal_camera(self):
        for model in ("sentinel_pro", "sentinel_lite"):
            result = acc.evaluate(acc.PROFILE_INDEX["sentinel_burn_monitor_kit"], catalog.MODEL_INDEX[model])
            self.assertEqual(result["verdict"], "not_compatible", model)
            self.assertIn("Thermal imaging", next(c for c in result["checks"] if c["id"] == "sensors")["detail"])

    def test_ignis_iii_mini_is_never_compatible_and_leaves_little_margin(self):
        result = acc.evaluate(acc.PROFILE_INDEX["ignis_iii_mini"], catalog.MODEL_INDEX["sentinel_thermal"])
        self.assertEqual(result["verdict"], "not_compatible")
        states = {check["id"]: check["state"] for check in result["checks"]}
        self.assertEqual(states["monitoring_only"], "fail")
        self.assertEqual(states["data"], "fail")
        self.assertEqual(states["mass"], "warn")  # under the 70%-throttle limit, above the 80% planning limit
        self.assertGreater(result["mass"]["fraction_of_limit"], 0.9)
        self.assertLess(result["mass"]["margin_g"], 600)

    def test_endurance_scales_with_mass_and_measured_mass_wins(self):
        model = catalog.MODEL_INDEX["sentinel_thermal"]
        light = acc.endurance_with_payload(model, 250)
        heavy = acc.endurance_with_payload(model, 1870)
        self.assertLess(heavy["with_payload_min"], light["with_payload_min"])
        self.assertAlmostEqual(light["with_payload_min"], 22 * (3.9 / 4.15) ** 1.5, places=1)
        measured = acc.evaluate(acc.PROFILE_INDEX["simulated_burn_monitor_kit"], model, measured_mass_g=900)
        self.assertEqual(measured["mass"]["accessory_basis"], "measured")
        self.assertEqual(measured["mass"]["takeoff_g"], 4800)
        adjusted = acc.model_with_payload(model, 250, "kit")
        self.assertLess(adjusted["flight"]["endurance_min_est"], model["flight"]["endurance_min_est"])
        self.assertEqual(model["flight"]["endurance_min_est"], 22.0)  # the catalog itself is untouched
        self.assertIs(acc.model_with_payload(model, 0, "none"), model)

    def test_profiles_carry_sources_and_ignition_payloads_cannot_be_installed(self):
        for profile in acc.PROFILES:
            self.assertIn(profile["mass"]["basis"], ("estimate", "published", "not published"))
            if profile["mass"]["basis"] == "published":
                self.assertTrue(profile["mass"]["source"])
            if profile["ignition"]:
                self.assertEqual(profile["support"], "unsupported")
                self.assertEqual(profile["applications"], [])
        self.assertTrue(all(p["interface"]["control"].startswith("none") for p in acc.PROFILES if not p["ignition"]))

    def test_registry_validation(self):
        from sentinel.config import validate_config

        config = load_config(FARM, resolve_secrets=False)
        validate_config(copy.deepcopy(config))
        for change, message in (
            (lambda c: c["devices"][2].update(profile="ignis_iii_mini", simulated=False), "not supported"),
            (lambda c: c["devices"][2].pop("drone_id"), "drone_id must name the drone"),
            (lambda c: c["devices"][2].update(drone_id="farm_dock"), "unknown drone"),
            (lambda c: c["devices"].append(dict(c["devices"][2], id="second_kit")), "already has accessory"),
        ):
            broken = copy.deepcopy(config)
            change(broken)
            with self.assertRaises(Exception) as caught:
                validate_config(broken)
            self.assertIn(message, str(caught.exception))


class FieldAndOperationRuleTests(unittest.TestCase):
    ORIGIN = (49.9, -97.6)

    def field(self, **extra):
        payload = {"name": "North quarter", "boundary_map": FIELD_MAP, "watch_buffer_m": 60, "fireguard_m": 8} | extra
        return fieldlib.clean_field(payload, site_origin=self.ORIGIN, taken=set(), actor="t")

    def test_field_geometry(self):
        view = fieldlib.view(self.field(), self.ORIGIN)
        self.assertAlmostEqual(view["area_ha"], 9.6, places=1)
        self.assertAlmostEqual(view["perimeter_m"], 1280, delta=2)
        self.assertEqual(view["boundary_map"][0], [100.0, 60.0])
        self.assertEqual([a["id"] for a in view["monitored_areas"]], ["interior", "edge", "watch_strip"])

    def test_field_validation(self):
        with self.assertRaises(fieldlib.FieldError):
            self.field(boundary_map=[[0, 0], [100, 0]])
        with self.assertRaises(fieldlib.FieldError):
            self.field(boundary_map=[[0, 0], [100, 100], [100, 0], [0, 100]])  # crosses itself
        with self.assertRaises(fieldlib.FieldError):
            self.field(boundary_map=[[0, 0], [5, 0], [5, 5]])  # smaller than 100 m²
        with self.assertRaises(fieldlib.FieldError):
            self.field(sensitive=[{"kind": "castle", "point": [49.9, -97.6]}])
        closed = self.field(boundary=[[49.9, -97.6], [49.9, -97.59], [49.91, -97.59], [49.9, -97.6]], boundary_map=None)
        self.assertEqual(len(closed["boundary"]), 3)

    def test_classification_respects_uncertainty(self):
        field = {"edge_m": 15.0, "watch_buffer_m": 60.0}
        self.assertEqual(ops.classify(-40, 3, field)["zone"], "interior")
        self.assertEqual(ops.classify(-10, 3, field)["zone"], "edge")
        self.assertEqual(ops.classify(5, 8, field)["zone"], "edge")  # within its position uncertainty of the boundary
        self.assertEqual(ops.classify(9, 3, field)["zone"], "outside")
        self.assertTrue(ops.classify(80, 3, field)["beyond_watch_strip"])

    def test_weather_against_the_burn_bosss_limits(self):
        limits = dict(ops.EXAMPLE_PRESCRIPTION) | {"source": "burn_boss"}
        self.assertEqual(ops.assess_weather({"wind_mps": 4, "wind_from_deg": 270, "humidity_pct": 40}, limits)["state"], "pass")
        bad = ops.assess_weather({"wind_mps": 9, "gust_mps": 12, "humidity_pct": 15, "wind_from_deg": 270}, limits)
        self.assertEqual(bad["state"], "fail")
        self.assertEqual(len(bad["problems"]), 3)
        self.assertEqual(ops.assess_weather(None, limits)["state"], "missing")
        example = ops.assess_weather({"wind_mps": 4, "wind_from_deg": 0, "humidity_pct": 40}, dict(ops.EXAMPLE_PRESCRIPTION, source="example"))
        self.assertIn("example limits", example["detail"])
        for text in (bad["detail"], example["detail"]):
            self.assertEqual(ops.forbidden_claims(text), [])

    def test_smoke_direction(self):
        field = fieldlib.view(self.field(sensitive=[{"kind": "home", "label": "Farmhouse", "point_map": [300, 425]}]), self.ORIGIN)
        limits = dict(ops.EXAMPLE_PRESCRIPTION)
        self.assertEqual(ops.smoke_toward(field, 0, limits)["state"], "fail")  # wind from the north carries smoke south to the farmhouse
        self.assertEqual(ops.smoke_toward(field, 180, limits)["state"], "pass")
        self.assertEqual(ops.smoke_toward(field, None, limits)["state"], "missing")

    def test_records_and_validity(self):
        today = date.today().isoformat()
        daily = ops.authorization({"kind": "daily_authorization", "valid_from": today, "valid_until": today, "reference": "MB-1"}, "farmer")
        self.assertTrue(ops.valid_on(daily, today))
        self.assertFalse(ops.valid_on(daily, "2999-01-01"))
        with self.assertRaises(ops.OperationError):
            ops.authorization({"kind": "daily_authorization", "valid_from": "2026-10-01", "valid_until": "2026-10-02"}, "farmer")
        with self.assertRaises(ops.OperationError):
            ops.authorization({"kind": "burn_permit"}, "farmer")  # a permit needs its expiry
        expired = ops.qualification({"kind": "rpas_basic", "person": "Sam", "expires": "2020-01-01"}, "farmer")
        self.assertFalse(ops.valid_on(expired, today))
        operation = {"crew": [{"name": "Sam", "role": "drone_pilot"}]}
        self.assertEqual(ops.pilot_state(operation, [expired], today)["state"], "missing")
        current = ops.qualification({"kind": "rpas_advanced", "person": "sam", "expires": "2999-01-01"}, "farmer")
        state = ops.pilot_state(operation, [current], today)
        self.assertEqual(state["state"], "pass")
        self.assertIn("not verified by Sentinel", state["detail"])

    def test_readiness_never_claims_approval_or_safety(self):
        operation = ops.new_operation({"id": "f", "name": "F"}, "d", "farmer", simulated=True)
        readiness = ops.readiness(operation, {})
        self.assertIn(ops.READINESS_NOTE, readiness["statement"])
        complete = {item["auto"]: {"state": "pass", "detail": "ok"} for item in ops.CHECKLIST if item.get("auto")}
        for item in ops.CHECKLIST:
            if not item.get("auto"):
                operation["checklist"][item["id"]] = {"status": "done", "by": "farmer"}
        readiness = ops.readiness(operation, complete)
        self.assertEqual(readiness["incomplete"], [])
        self.assertIn("does not approve burns", readiness["statement"])
        self.assertEqual(ops.forbidden_claims(readiness["statement"] + ops.DISCLAIMER), [])

    def test_claim_scanner(self):
        self.assertTrue(ops.forbidden_claims("Conditions are good: it is safe to light the north edge"))
        self.assertTrue(ops.forbidden_claims("The fire is out."))
        self.assertTrue(ops.forbidden_claims("Your burn is approved."))
        self.assertFalse(ops.forbidden_claims("Heat 20 m outside the boundary; if a fire is out of control call 911"))


class SimulatedFireTests(unittest.TestCase):
    def boundary(self):
        return [list(map_point_to_geo((49.9, -97.6), x, y)) for x, y in FIELD_MAP]

    def test_fire_spreads_downwind_respects_the_fireguard_and_cools(self):
        fire = SimulatedFireField(self.boundary(), fireguard_m=8, wind_from_deg=0, wind_mps=4, seed=3)
        self.assertGreater(fire.ignite_edge(0), 10)
        for _ in range(60):
            fire.step(5)
        self.assertGreater(fire.stats()["burnt_share"], 0.5)
        self.assertEqual(fire.stats()["flaming_m2_outside"], 0)  # outside fuel is only lit by spot fires
        guard = [i for i, s in enumerate(fire.signed_m) if -8 < s < 0]
        self.assertTrue(all(fire.state[i] == 0 for i in guard))
        burnt = next(i for i, s in enumerate(fire.state) if s == 2)
        before = fire.temperature(burnt)
        fire.step(600)
        self.assertLess(fire.temperature(burnt), before)

    def test_same_seed_same_fire(self):
        runs = []
        for _ in range(2):
            fire = SimulatedFireField(self.boundary(), wind_from_deg=270, wind_mps=3, seed=11)
            fire.ignite_edge(90)
            for _ in range(20):
                fire.step(5)
            runs.append(fire.stats())
        self.assertEqual(runs[0], runs[1])


class FlameServiceTests(unittest.TestCase):
    def test_emergency_notification_survives_failed_database_writes(self):
        import sqlite3
        from unittest.mock import patch

        oid = self.ready_operation()
        operation = self.flame.operations[oid]
        with patch.object(self.service.runtime.store, "_write", side_effect=sqlite3.OperationalError("database full")):
            row = self.flame.emergency_alert(oid, "Crew reports heat beyond the field", "Pat")
        self.assertFalse(row["recorded"])
        self.assertTrue(any("Crew reports heat" in item.message for item in self.notifications))
        self.assertGreater(self.flame.record_failures, 0)
        self.assertEqual(self.service.runtime.health.components["flame_records"]["state"], "degraded")
        self.assertTrue(self.flame.overview()["recording"]["history_incomplete"])
        self.assertEqual(operation["counts"]["notifications"], 1)

    def test_damaged_settings_are_preserved_before_a_later_save(self):
        import hashlib

        for damaged in ("{broken", "[]", '{"fields": []}'):
            with self.subTest(damaged=damaged):
                self.flame.path.write_text(damaged, encoding="utf-8")
                self.flame.doc = self.flame._load()
                self.flame._save()
                saved = [path for path in (self.flame.path.parent / "recovery").glob("flame.*.damaged.json")
                         if path.read_text(encoding="utf-8") == damaged]
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0].with_suffix(".sha256").read_text(), hashlib.sha256(damaged.encode()).hexdigest())
                self.assertEqual(json.loads(self.flame.path.read_text())["fields"], {})

    def setUp(self):
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        self.service = SentinelService(farm_config(self.directory.name), start_cameras=False)
        self.notifications = []
        self.service.runtime.dispatcher.submit = self.notifications.append
        self.flame = self.service.flame
        self.drone = self.service.drone

    def tearDown(self):
        self.service.stop()
        time.sleep(0.05)
        self.directory.cleanup()

    # ---- helpers ------------------------------------------------------------------------

    def commission(self):
        self.flame.add_accessory_record("burn_kit", {"kind": "commissioning", "measured_mass_g": 262, "cg_checked": True}, "engineer")
        self.flame.add_accessory_record("burn_kit", {"kind": "inspection"}, "engineer")

    def agricultural(self):
        self.commission()
        self.flame.set_configuration("farm_drone", "agricultural_monitoring", "farmer")

    def field(self, **extra):
        return self.flame.save_field(None, {"name": "North quarter", "boundary_map": FIELD_MAP, "watch_buffer_m": 60, "fireguard_m": 8,
                                            "crop": "wheat stubble",
                                            "sensitive": [{"kind": "home", "label": "Farmhouse", "point_map": [300, 425]}]} | extra, "farmer")

    def operation(self):
        field = self.field()
        op = self.flame.create_operation(field["id"], "farmer")
        return op["operation_id"]

    def ready_operation(self):
        self.agricultural()
        oid = self.operation()
        today = date.today().isoformat()
        self.flame.add_authorization({"kind": "daily_authorization", "reference": "MB-1", "valid_from": today, "valid_until": today}, "farmer")
        self.flame.add_qualification({"kind": "rpas_advanced", "person": "Sam Pilot", "expires": "2999-01-01"}, "farmer")
        view = self.flame.operation_view(oid)
        self.flame.update_operation(oid, {
            "crew": [{"name": "Pat", "role": "burn_boss"}, {"name": "Sam Pilot", "role": "drone_pilot"}, {"name": "Lee", "role": "holding"}],
            "prescription": {"max_wind_mps": 7}, "equipment": [{"id": e["id"], "checked": True} for e in view["equipment"]],
            "checklist": {i["id"]: {"status": "done"} for i in ops.CHECKLIST if not i.get("auto")}}, "farmer")
        self.flame.add_weather(oid, {"wind_mps": 4, "wind_from_deg": 180, "humidity_pct": 40, "temperature_c": 18}, "farmer")
        return oid

    def fly_watch(self, oid, side="north", steps=150):
        request = self.flame.request_watch(oid, side, "farmer")
        self.drone.approve(request["request_id"], "farmer")
        for _ in range(steps):
            self.drone.step(1.0)
            self.flame.step(1.0)
        return request

    def advance(self, steps):
        for _ in range(steps):
            self.drone.step(1.0)
            self.flame.step(1.0)

    def kinds(self, oid):
        return [o["kind"] for o in self.flame.operation_view(oid, observations=5000)["observations"]]

    # ---- accessory and configuration ----------------------------------------------------

    def test_accessory_registered_with_live_simulated_health(self):
        view = self.flame.accessory("burn_kit")
        self.assertEqual((view["state"], view["attachment"], view["support"]), ("ok", "attached", "simulated"))
        self.assertEqual(view["compatibility"]["verdict"], "needs_validation")
        self.assertTrue(view["health"]["identity_ok"])
        device = next(d for d in self.service.devices_view() if d["id"] == "burn_kit")
        self.assertEqual((device["category"], device["level"]), ("accessory", "connected"))
        self.assertIn("burn_monitoring", device["capabilities"])

    def test_planned_accessory_is_registration_only(self):
        self.service.site.save_device("burn_kit", {"profile": "sentinel_burn_monitor_kit", "simulated": False}, "farmer")
        view = self.flame.accessory("burn_kit")
        self.assertEqual((view["state"], view["attachment"]), ("not_connected", "unknown"))
        with self.assertRaisesRegex(Exception, "registration only"):
            self.flame.add_accessory_record("burn_kit", {"kind": "commissioning", "measured_mass_g": 250, "cg_checked": True}, "engineer")
        device = next(d for d in self.service.devices_view() if d["id"] == "burn_kit")
        self.assertEqual((device["level"], device["state"]), ("registered", "not_connected"))

    def test_switching_needs_commissioning_and_inspection(self):
        blockers = {b["id"] for b in self.flame.configuration_view("farm_drone")["switch_blockers"]}
        self.assertEqual(blockers, {"accessory_not_commissioned", "accessory_inspection_due"})
        with self.assertRaises(FlightCommandRejected):
            self.flame.set_configuration("farm_drone", "agricultural_monitoring", "farmer")
        with self.assertRaisesRegex(Exception, "measured_mass_g"):
            self.flame.add_accessory_record("burn_kit", {"kind": "commissioning", "cg_checked": True}, "engineer")
        with self.assertRaisesRegex(Exception, "cg_checked"):
            self.flame.add_accessory_record("burn_kit", {"kind": "commissioning", "measured_mass_g": 260}, "engineer")
        self.agricultural()
        self.assertEqual(self.flame.configuration("farm_drone"), "agricultural_monitoring")
        audit = [row["action"] for row in self.service.runtime.store.list_audit(50)]
        self.assertIn("drone_configuration_changed", audit)
        self.assertEqual(self.flame.accessory("burn_kit")["mass_basis"], "measured")

    def test_configurations_keep_security_and_agricultural_work_apart(self):
        with self.assertRaisesRegex(FlightCommandRejected, "security configuration"):
            self.drone.request_inspection(requested_by="farmer", map_point=(300, 100), label="field", source="flame")
        security = self.drone.request_inspection(requested_by="farmer", map_point=(300, 380), label="yard")
        blockers = {b["id"] for b in self.flame.configuration_view("farm_drone")["switch_blockers"]}
        self.assertIn("security_requests_open", blockers)
        self.drone.deny(security.request_id, "farmer")
        self.agricultural()
        with self.assertRaisesRegex(FlightCommandRejected, "agricultural monitoring configuration"):
            self.drone.request_inspection(requested_by="sentinel", map_point=(300, 380), label="yard", source="incident")
        checks = {c["id"]: c for c in self.drone.readiness()["checks"]}
        self.assertEqual(checks["configuration"]["detail"], "Agricultural monitoring")
        self.assertEqual(checks["accessory"]["state"], "pass")
        self.assertLess(self.drone.model["flight"]["endurance_min_est"], catalog.MODEL_INDEX["sentinel_thermal"]["flight"]["endurance_min_est"])
        self.flame.set_configuration("farm_drone", "security", "farmer")
        checks = {c["id"]: c for c in self.drone.readiness()["checks"]}
        self.assertEqual(checks["accessory"]["state"], "warn")  # still fitted: heavier aircraft in the security configuration
        self.drone.request_inspection(requested_by="farmer", map_point=(300, 380), label="yard")

    def test_detached_accessory_fails_readiness_and_blocks_switching(self):
        self.agricultural()
        self.flame.simulate_accessory("burn_kit", "detach", "tester")
        self.flame.step(1.0)
        checks = {c["id"]: c for c in self.drone.readiness()["checks"]}
        self.assertEqual(checks["accessory"]["state"], "fail")
        self.assertIn("accessory_not_attached", self.drone.readiness()["blockers"])
        self.assertEqual(self.drone.model["flight"]["endurance_min_est"], 22.0)  # nothing fitted, base endurance again
        records = [r["kind"] for r in self.flame.accessory("burn_kit")["records"]]
        self.assertIn("detached", records)
        self.assertFalse(self.flame.accessory("burn_kit")["inspection_due"])  # removing the kit is routine, not a fault
        self.flame.simulate_accessory("burn_kit", "attach", "tester")
        self.flame.step(1.0)
        self.assertIn("attached", [r["kind"] for r in self.flame.accessory("burn_kit")["records"]])
        self.flame.simulate_accessory("burn_kit", "latch_open", "tester")
        self.flame.simulate_accessory("burn_kit", "clear", "tester")
        self.assertTrue(self.flame.accessory("burn_kit")["inspection_due"])  # a fault since the last inspection

    def test_cannot_switch_in_the_air_or_during_an_operation(self):
        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer", acknowledge_incomplete=True)
        blockers = {b["id"] for b in self.flame.configuration_view("farm_drone")["switch_blockers"]}
        self.assertIn("burn_operation_active", blockers)
        self.fly_watch(oid, steps=40)
        blockers = {b["id"] for b in self.flame.configuration_view("farm_drone")["switch_blockers"]}
        self.assertIn("aircraft_not_on_ground", blockers)

    def test_removing_the_drone_removes_its_accessory(self):
        result = self.service.remove_device("farm_drone", "farmer")
        self.assertFalse(any(d.get("category") == "accessory" for d in self.service.site.config["devices"]))
        self.assertTrue(any("burn_kit" in note for note in result.get("notes", [])))

    # ---- fields and operations ------------------------------------------------------------

    def test_field_outside_the_geofence_is_flagged(self):
        field = self.field(name="Far field", boundary_map=[[700, 60], [900, 60], [900, 200], [700, 200]])
        self.assertEqual(field["geofence"]["state"], "fail")
        self.assertEqual(self.field()["geofence"]["state"], "pass")

    def test_mark_corners_with_the_drone(self):
        position = self.flame.drone_position("farm_drone")
        self.assertAlmostEqual(position["lat"], map_point_to_geo((49.9, -97.6), 300, 400)[0], places=5)

    def test_checklist_starts_open_and_monitoring_needs_acknowledgement(self):
        oid = self.operation()
        view = self.flame.operation_view(oid)
        self.assertGreater(len(view["readiness"]["incomplete"]), 10)
        with self.assertRaisesRegex(Exception, "acknowledge"):
            self.flame.start_monitoring(oid, "farmer")
        with self.assertRaisesRegex(Exception, "not a checklist item a person confirms"):
            self.flame.update_operation(oid, {"checklist": {"weather": {"status": "done"}}}, "farmer")
        with self.assertRaisesRegex(Exception, "say why"):
            self.flame.update_operation(oid, {"checklist": {"neighbours": {"status": "not_applicable"}}}, "farmer")
        with self.assertRaisesRegex(Exception, "already has an open operation"):
            self.flame.create_operation(view["field_id"], "farmer")

    def test_complete_checklist(self):
        oid = self.ready_operation()
        readiness = self.flame.operation_view(oid)["readiness"]
        self.assertEqual(readiness["incomplete"], [], readiness["items"])
        self.assertEqual(ops.forbidden_claims(json.dumps(readiness)), [])

    def test_smoke_toward_the_farmhouse_keeps_the_checklist_open(self):
        oid = self.ready_operation()
        self.flame.add_weather(oid, {"wind_mps": 4, "wind_from_deg": 0, "humidity_pct": 40}, "farmer")
        readiness = self.flame.operation_view(oid)["readiness"]
        self.assertIn("smoke", readiness["incomplete"])

    def test_spot_fire_outside_the_boundary_alerts_people(self):
        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer")
        self.fly_watch(oid, steps=90)
        self.flame.report_ignition(oid, {"side": "north"}, "Pat")
        self.flame.simulate(oid, {"action": "spot_fire", "side": "north", "distance_m": 25}, "tester")
        self.advance(30)
        kinds = self.kinds(oid)
        self.assertIn("heat_outside_boundary", kinds)
        outside = next(o for o in self.flame.operation_view(oid, observations=5000)["observations"] if o["kind"] == "heat_outside_boundary")
        self.assertEqual(outside["severity"], "critical")
        self.assertEqual(outside["zone"], "outside")
        self.assertIsNotNone(outside["hotspot"]["uncertainty_m"])
        alerts = [n for n in self.notifications if n.kind == "flame" and "outside" in n.title]
        self.assertTrue(alerts)
        self.assertTrue(alerts[0].title.startswith("[SIMULATED]"))
        self.assertIn("call 911", alerts[0].message)
        count = len(alerts)
        self.advance(30)
        self.assertLessEqual(len([n for n in self.notifications if "outside" in n.title]), count + 2)  # rate-limited per place
        coverage = self.flame.operation_view(oid)["coverage"]
        self.assertTrue(coverage["available"])
        self.assertGreater(coverage["strip_pct"], 0)

    def test_heat_unseen_when_the_camera_looks_elsewhere(self):
        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer")
        self.fly_watch(oid, side="south", steps=90)
        self.flame.simulate(oid, {"action": "spot_fire", "side": "north", "distance_m": 25}, "tester")
        self.advance(20)
        self.assertNotIn("heat_outside_boundary", self.kinds(oid))
        self.assertLess(self.flame.operation_view(oid)["coverage"]["strip_pct"], 50)

    def test_faults_during_monitoring_reach_people(self):
        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer")
        self.fly_watch(oid, steps=60)
        self.flame.simulate(oid, {"action": "thermal_fault", "on": True}, "tester")
        self.advance(2)
        self.flame.simulate_accessory("burn_kit", "latch_open", "tester")
        self.advance(2)
        self.drone.adapter.set_link(False)
        self.advance(2)
        self.drone.adapter.set_link(True)
        self.drone.adapter.battery = 25.0
        self.advance(3)
        kinds = self.kinds(oid)
        for kind in ("thermal_feed_lost", "accessory_fault", "drone_safety"):
            self.assertIn(kind, kinds)
        safety = [o["safety_state"] for o in self.flame.operation_view(oid, observations=5000)["observations"] if o["kind"] == "drone_safety" and o.get("safety_state")]
        self.assertIn("link_lost", safety)
        self.assertIn("low_battery_return", safety)
        titles = " | ".join(n.title for n in self.notifications)
        self.assertIn("Thermal watch lost", titles)
        self.assertIn("accessory fault", titles)
        self.assertIn("Drone problem", titles)

    def test_post_burn_inspection_and_report(self):
        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer")
        self.flame.report_ignition(oid, {"side": "north"}, "Pat")
        self.fly_watch(oid, steps=60)
        self.flame.simulate(oid, {"action": "advance", "seconds": 600}, "tester")
        self.flame.start_post_burn(oid, "farmer")
        self.advance(20)
        with self.assertRaisesRegex(Exception, "post-burn result"):
            self.flame.close_operation(oid, {}, "farmer")
        self.flame.close_operation(oid, {"result": "heat_remains", "note": "crew stays"}, "farmer")
        report = self.flame.report(oid)
        self.assertTrue(report["simulated"])
        self.assertEqual(report["operation"]["post_burn"]["result"], "heat_remains")
        self.assertEqual(report["readiness"]["at"], "monitoring start")
        self.assertEqual(len(report["fingerprint"]), 64)
        self.assertTrue(report["drone"]["flights"])
        self.assertTrue(report["authorizations"])
        self.assertTrue(report["qualifications"])
        from sentinel.flame.reports import render_html

        html = render_html(report)
        self.assertIn("SIMULATED OPERATION", html)
        self.assertEqual(ops.forbidden_claims(html), [])
        self.assertIn(oid, [o["operation_id"] for o in self.flame.operations_list()])
        self.assertNotIn(oid, self.flame.operations)
        operation = self.service.runtime.store.list_burn_operations(limit=5)[0]
        self.assertEqual(len(operation["report_fingerprint"]), 64)

    def test_every_generated_text_avoids_forbidden_claims(self):
        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer")
        self.flame.report_ignition(oid, {"side": "north"}, "Pat")
        self.fly_watch(oid, steps=60)
        self.flame.simulate(oid, {"action": "spot_fire", "side": "north", "distance_m": 25}, "tester")
        self.advance(20)
        self.flame.stand_down(oid, "wind rising", "Pat")
        texts = [o["text"] for o in self.flame.operation_view(oid, observations=5000)["observations"]]
        texts += [f"{n.title} {n.message}" for n in self.notifications]
        texts.append(json.dumps(self.flame.overview()))
        for text in texts:
            self.assertEqual(ops.forbidden_claims(text), [], text)

    def test_restart_keeps_the_operation_and_marks_the_gap(self):
        from sentinel.flame import FlameService

        oid = self.ready_operation()
        self.flame.start_monitoring(oid, "farmer", acknowledge_incomplete=True)
        restarted = FlameService(self.service)
        self.assertIn(oid, restarted.operations)
        self.assertIn("monitoring_interrupted", [o["kind"] for o in restarted.operation_view(oid)["observations"]])
        self.assertEqual(restarted.configuration("farm_drone"), "agricultural_monitoring")

    def test_backup_includes_flame_settings(self):
        self.field()
        archive = self.service.create_backup(label="test", actor="tester")
        import zipfile

        path = Path(self.directory.name) / "backups" / archive["name"]
        with zipfile.ZipFile(path) as zipped:
            self.assertIn("flame.json", zipped.namelist())


@unittest.skipIf(TestClient is None, "fastapi not installed")
class FlameApiAndGuideTests(unittest.TestCase):
    def setUp(self):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        self.service = SentinelService(farm_config(self.directory.name), start_cameras=False)
        self.service.runtime.dispatcher.submit = lambda notification: None
        self.client = TestClient(create_app(self.service, api_token=None))
        self.cid = self.client.post("/api/v1/assistant/conversations").json()["id"]

    def tearDown(self):
        self.service.stop()
        time.sleep(0.05)
        self.directory.cleanup()

    def say(self, text):
        response = self.client.post(f"/api/v1/assistant/conversations/{self.cid}/messages", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return [m for m in response.json()["messages"] if m["role"] == "assistant"][-1]

    def test_api_flow(self):
        overview = self.client.get("/api/v1/flame").json()
        self.assertTrue(overview["enabled"])
        self.assertEqual(overview["accessories"][0]["id"], "burn_kit")
        matrix = self.client.get("/api/v1/flame/compatibility").json()["matrix"]
        self.assertEqual(len(matrix), len(acc.PROFILES) * 3)
        self.assertEqual(self.client.put("/api/v1/flame/drones/farm_drone/configuration", json={"configuration": "agricultural_monitoring"}).status_code, 409)
        record = {"kind": "commissioning", "measured_mass_g": 255, "cg_checked": True}
        self.assertEqual(self.client.post("/api/v1/flame/accessories/burn_kit/records", json=record).status_code, 200)
        self.client.post("/api/v1/flame/accessories/burn_kit/records", json={"kind": "inspection"})
        self.assertEqual(self.client.put("/api/v1/flame/drones/farm_drone/configuration", json={"configuration": "agricultural_monitoring"}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/flame/fields", json={"name": "Bad", "boundary_map": [[0, 0], [1, 1]]}).status_code, 422)
        field = self.client.post("/api/v1/flame/fields", json={"name": "North quarter", "boundary_map": FIELD_MAP}).json()
        op = self.client.post("/api/v1/flame/operations", json={"field_id": field["id"]}).json()
        oid = op["operation_id"]
        self.assertEqual(self.client.patch(f"/api/v1/flame/operations/{oid}", json={"crew": [{"name": "Pat", "role": "burn_boss"}]}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/v1/flame/operations/{oid}", json={"ignite": True}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/v1/flame/operations/{oid}/weather", json={"wind_mps": 3, "wind_from_deg": 270}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/flame/operations/{oid}/start", json={}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/v1/flame/operations/{oid}/start", json={"acknowledge_incomplete": True}).status_code, 200)
        watch = self.client.post(f"/api/v1/flame/operations/{oid}/watch", json={"side": "east"}).json()
        self.assertEqual(watch["source"], "flame")
        self.assertEqual(watch["hover_s"], 180.0)
        html = self.client.get(f"/api/v1/flame/operations/{oid}/report?format=html")
        self.assertEqual(html.status_code, 200)
        self.assertIn("text/html", html.headers["content-type"])
        paths = [route.path for route in self.client.app.routes if route.path.startswith("/api/v1/flame")]
        self.assertFalse([p for p in paths if re.search(r"ignit(e|ion)/(start|fire|release)|release|drop", p)])

    def test_guide_answers_the_farmers_questions(self):
        self.service.flame.add_accessory_record("burn_kit", {"kind": "commissioning", "measured_mass_g": 255, "cg_checked": True}, "engineer")
        self.service.flame.add_accessory_record("burn_kit", {"kind": "inspection"}, "engineer")
        reply = self.say("Help me map this field.")
        self.assertIn("Draw it", reply["text"])
        reply = self.say("Help me map this field called Home quarter: 49.9000, -97.5990; 49.9000, -97.5960; 49.8980, -97.5960; 49.8980, -97.5990")
        proposal = next(card for card in reply["cards"] if card["type"] == "proposal")
        decided = self.client.post(f"/api/v1/assistant/conversations/{self.cid}/actions/{proposal['action_id']}", json={"confirm": True})
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(len(self.service.flame.doc["fields"]), 1)
        reply = self.say("Show the areas being monitored")
        self.assertIn("Watch strip", reply["text"])
        reply = self.say("Are all drone sensors working?")
        self.assertIn("Burn-monitoring kit", reply["text"])
        self.assertIn("not a flight clearance", reply["text"])
        field_id = next(iter(self.service.flame.doc["fields"]))
        self.service.flame.create_operation(field_id, "farmer")
        reply = self.say("What equipment checks are incomplete?")
        self.assertIn("Equipment not checked", reply["text"])
        reply = self.say("Can I start the burn now? Is it safe to light?")
        self.assertIn("burn boss's decision", reply["text"])
        self.assertEqual(ops.forbidden_claims(reply["text"]), [])
        reply = self.say("Are there any hotspots outside the expected area?")
        self.assertIn("No heat was observed outside", reply["text"])
        self.assertIn("not that there is none", reply["text"])
        reply = self.say("Generate a report of the operation.")
        self.assertIn("printable report", reply["text"])
        self.assertTrue(any(action["target"] == "flame_report" for action in reply["actions"]))
        reply = self.say("Check my firewall settings")
        self.assertNotIn("Watch strip", reply["text"])

    def test_tools_are_registered_for_any_ai_provider(self):
        names = {tool["name"] for tool in self.service.assistant.toolbox.definitions()}
        for name in ("get_flame_overview", "get_drone_sensor_status", "get_burn_checklist", "get_hotspots", "get_operation_report",
                     "propose_field", "propose_drone_configuration"):
            self.assertIn(name, names)
        self.assertFalse([n for n in names if re.search(r"ignit|release|drop", n)])
        from sentinel.assistant.core import SYSTEM_PROMPT

        self.assertIn("Never say or imply that a planned burn is legally approved", SYSTEM_PROMPT)

    def test_deepseek_reads_flame_and_proposes_a_field_without_saving_it(self):
        import httpx
        from sentinel.assistant.deepseek import DeepSeekProvider
        from test_sentinel_assistant import _completion, _tool_call

        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                message = {"content": "", "tool_calls": [
                    _tool_call("overview", "get_flame_overview", {}),
                    _tool_call("draft", "propose_field", {"name": "Provider field", "corners": [
                        [49.9, -97.6], [49.9, -97.599], [49.901, -97.599], [49.901, -97.6]], "watch_strip_m": 20}),
                ]}
            else:
                message = {"content": "The field is a proposal. Review its outline and confirm to save it."}
            return httpx.Response(200, json=_completion(message))

        provider = DeepSeekProvider(api_key="simulated-test-key", transport=httpx.MockTransport(respond))
        self.addCleanup(provider._client.close)
        self.service.assistant.override = provider
        reply = self.say("Prepare the field draft for my review")
        self.assertEqual(reply["source"], "ai")
        self.assertEqual(self.service.flame.doc["fields"], {})
        proposal = next(card for card in reply["cards"] if card["type"] == "proposal")
        tool_names = {tool["function"]["name"] for tool in requests[0]["tools"]}
        self.assertIn("get_hotspots", tool_names)
        self.assertIn("propose_field", tool_names)
        self.assertEqual(sum(message["role"] == "tool" for message in requests[1]["messages"]), 2)
        response = self.client.post(f"/api/v1/assistant/conversations/{self.cid}/actions/{proposal['action_id']}", json={"confirm": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "applied")
        self.assertEqual(next(iter(self.service.flame.doc["fields"].values()))["name"], "Provider field")


if __name__ == "__main__":
    unittest.main()
