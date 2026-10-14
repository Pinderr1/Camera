"""Installation: the device registry and hardware profiles, the property
planner, honest readiness (registered / connected / video / AI verified),
save and resume, drone-first sites without cameras, adding and removing
hardware later, notification tests, and backups of installation progress."""

import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sentinel import planner
from sentinel.config import load_config, validate_config
from sentinel.credentials import SecretStore
from sentinel.devices import PROFILES, camera_entry, device_entry
from sentinel.installation import InstallationManager, readiness
from sentinel.site_config import SiteConfigError, SiteConfigManager

try:
    from fastapi.testclient import TestClient

    from sentinel.api import create_app
    from sentinel.service import SentinelService
except ImportError:  # FastAPI is only needed for the service-level tests
    TestClient = None

CONFIG = load_config("config/sentinel.example.json", resolve_secrets=False)
DRONE_FIRST = load_config("config/sentinel.drone-first.example.json", resolve_secrets=False)
FORBIDDEN = ("protected", "secure", "safe from")


def clean(config):
    return {key: value for key, value in copy.deepcopy(config).items() if not key.startswith("_")}


def wait_for(predicate, timeout=20.0, message="condition not reached"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


class DeviceValidationTests(unittest.TestCase):
    def check(self, mutate):
        config = clean(CONFIG)
        mutate(config)
        validate_config(config)

    def rejects(self, mutate, fragment):
        with self.assertRaises(ValueError) as caught:
            self.check(mutate)
        self.assertIn(fragment, str(caught.exception))

    def test_a_site_may_have_no_cameras(self):
        validate_config(clean(DRONE_FIRST))
        config = clean(CONFIG)
        config["cameras"] = []
        config["rules"] = []
        config["topology"] = {"links": []}
        validate_config(config)

    def test_profiles_are_complete_and_honest(self):
        for profile in PROFILES:
            for key in ("id", "category", "name", "support", "protocols", "capabilities", "notes"):
                self.assertIn(key, profile, profile["id"])
            self.assertIn(profile["support"], ("verified", "simulated", "expected", "limited", "planned", "unsupported"))
            self.assertNotIn("price", json.dumps(profile).lower())
        # Nothing but this computer is claimed as verified on physical hardware yet.
        self.assertEqual([p["id"] for p in PROFILES if p["support"] == "verified"], ["this_computer"])
        self.assertTrue(any(p["support"] == "unsupported" for p in PROFILES if p["category"] == "camera"))

    def test_device_rules(self):
        self.rejects(lambda c: c.setdefault("devices", []).append({"id": "cam_front", "category": "recorder", "name": "x", "profile": "generic_recorder"}),
                     "site-wide")
        self.rejects(lambda c: c.setdefault("devices", []).append({"id": "r1", "category": "recorder", "name": "x", "profile": "simulated_drone"}),
                     "recorder profile")
        self.rejects(lambda c: c.setdefault("devices", []).append({"id": "s1", "category": "contact_sensor", "name": "x", "profile": "mqtt_sensor", "simulated": True}),
                     "only simulator profiles")
        self.rejects(lambda c: c.setdefault("devices", []).append({"id": "d1", "category": "drone", "name": "x", "profile": "mavlink_drone", "adapter": "mavlink"}),
                     "connection is required")
        self.rejects(lambda c: c.setdefault("devices", []).append({"id": "d1", "category": "drone", "name": "x", "profile": "mavlink_drone", "adapter": "mavlink",
                                                                    "connection": "tcp://user:secret@10.0.0.5:5760"}), "must not embed a password")
        self.rejects(lambda c: c["cameras"][0].update(recorder="nope"), "unknown recorder")
        self.rejects(lambda c: c["drone"].update(device="ghost"), "unknown drone")
        self.rejects(lambda c: c["drone"].pop("geofence"), "geofence")
        self.rejects(lambda c: c["site"]["map"].pop("origin_geo"), "origin_geo")
        self.rejects(lambda c: c["alerts"]["channels"].__setitem__(0, {"type": "ntfy", "enabled": True}), "needs a topic")
        self.rejects(lambda c: c["alerts"]["channels"].__setitem__(0, {"type": "ntfy", "topic": "abcdef", "server": "https://u:p@ntfy.sh"}), "without credentials")


class SiteDeviceEditingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.secrets = SecretStore(Path(self.directory.name) / "credentials.json")
        self.site = SiteConfigManager(clean(CONFIG), self.directory.name, self.secrets)
        self.applied = []
        self.site.subscribe(self.applied.append)

    def tearDown(self):
        self.directory.cleanup()

    def test_register_edit_and_remove_with_cascades(self):
        recorder = self.site.save_device(None, {"category": "recorder", "name": "Garage NVR", "profile": "hikvision_recorder", "host": "192.168.1.84", "channels": 8}, "t")
        self.assertEqual(recorder["id"], "garage_nvr")
        self.site.save_camera("cam_front", {"recorder": "garage_nvr"}, "t")
        drone = self.site.save_device(None, {"category": "drone", "name": "Yard drone", "simulated": True, "profile": "simulated_drone"}, "t")
        self.assertEqual(drone["adapter"], "simulated")
        self.site.set_drone({"device": drone["id"]}, "t")
        dock = self.site.save_device(None, {"category": "dock", "name": "Dock", "simulated": True, "drone_id": drone["id"]}, "t")
        self.assertEqual(dock["profile"], "simulated_dock")
        renamed = self.site.save_device(drone["id"], {"name": "Back drone"}, "t")
        self.assertEqual((renamed["name"], renamed["category"]), ("Back drone", "drone"))

        result = self.site.delete_device("garage_nvr", "t")
        self.assertNotIn("recorder", next(c for c in self.site.config["cameras"] if c["id"] == "cam_front"))
        self.assertTrue(result["notes"])
        self.site.delete_device(drone["id"], "t")
        config = self.site.config
        self.assertFalse(config["drone"]["enabled"], "drone features must not fall back to an unregistered drone")
        self.assertNotIn("device", config["drone"])
        self.assertNotIn("drone_id", next(d for d in config["devices"] if d["id"] == dock["id"]))

    def test_cameras_cannot_be_registered_as_devices_and_removed_zones_unassign_devices(self):
        with self.assertRaises(SiteConfigError):
            self.site.save_device(None, {"category": "camera", "name": "x"}, "t")
        sensor = self.site.save_device(None, {"category": "contact_sensor", "name": "Front door", "simulated": True, "zone_id": "front_porch"}, "t")
        camera = next(c for c in self.site.config["cameras"] if any(z["id"] == "front_porch" for z in c.get("zones", [])))
        self.site.delete_camera(camera["id"], "t")
        self.assertNotIn("zone_id", next(d for d in self.site.config["devices"] if d["id"] == sensor["id"]))

    def test_every_camera_can_be_removed(self):
        for camera in list(self.site.config["cameras"]):
            self.site.delete_camera(camera["id"], "t")
        self.assertEqual(self.site.config["cameras"], [])
        self.assertFalse(any(rule.get("enabled", True) and rule.get("zones") for rule in self.site.config["rules"]))

    def test_ntfy_topic_lives_in_the_credential_store_only(self):
        with self.assertRaises(SiteConfigError):
            self.site.set_notifications({"ntfy": {"enabled": True, "topic": "short"}}, "t")
        self.site.set_notifications({"ntfy": {"enabled": True, "server": "https://ntfy.example", "topic": "maple-street-7f3k2"}}, "t")
        channel = next(c for c in self.site.config["alerts"]["channels"] if c["type"] == "ntfy")
        self.assertNotIn("topic", channel)
        self.assertEqual(self.secrets.get(channel["topic_credential"]), "maple-street-7f3k2")
        stored = (Path(self.directory.name) / "site.json").read_text(encoding="utf-8")
        self.assertNotIn("maple-street-7f3k2", stored)
        self.assertNotIn("maple-street-7f3k2", json.dumps(self.site.public_view()))
        self.site.set_notifications({"ntfy": {"enabled": True}}, "t")  # keeps the stored topic
        self.assertEqual(self.secrets.get("alerts.ntfy_topic"), "maple-street-7f3k2")

    def test_drone_setup_defaults_to_the_property_rectangle(self):
        config = clean(CONFIG)
        config["drone"] = {"enabled": False}
        site = SiteConfigManager(config, self.directory.name, self.secrets)
        drone = site.set_drone({"enabled": True, "inspection_points": [{"name": "Back gate", "map": [30, 20]}]}, "t")
        self.assertEqual(drone["geofence"]["map_polygon"], [[0, 0], [40, 0], [40, 30], [0, 30]])
        self.assertEqual(drone["inspection_points"][0]["id"], "back_gate")


class PlannerTests(unittest.TestCase):
    HOUSE = [[10, 10], [30, 10], [30, 20], [10, 20]]

    def plan(self, *items):
        return planner.clean_plan({"items": list(items)})

    def test_detection_range_follows_the_benchmark_rule(self):
        self.assertAlmostEqual(planner.detection_range_m(90), 15.1, delta=0.2)
        self.assertGreater(planner.detection_range_m(60), planner.detection_range_m(90))
        self.assertLess(planner.detection_range_m(110), planner.detection_range_m(90))

    def test_a_door_camera_sees_the_approach_and_buildings_block_views(self):
        plan = self.plan(
            {"kind": "building", "name": "House", "polygon": self.HOUSE},
            {"kind": "entrance", "name": "Front door", "point": [20, 20]},
            {"kind": "gate", "name": "Back gate", "point": [20, 3]},
            {"kind": "camera", "name": "Porch", "point": [21, 20.5], "heading_deg": 180, "fov_deg": 100},
        )
        result = planner.analyse_coverage(plan, 40, 30)
        features = {f["name"]: f for f in result["features"]}
        self.assertEqual(features["Front door"]["covered_by"], ["Porch"])
        self.assertEqual(features["Back gate"]["covered_by"], [])
        self.assertTrue(any("Back gate" in gap for gap in result["gaps"]))
        # A camera on the far side of the house, aimed through it, sees nothing behind the house.
        blocked = self.plan({"kind": "building", "name": "House", "polygon": self.HOUSE},
                            {"kind": "gate", "name": "Gate", "point": [20, 25]},
                            {"kind": "camera", "name": "Back", "point": [20, 5], "heading_deg": 180, "fov_deg": 40, "range_m": 40})
        self.assertEqual(planner.analyse_coverage(blocked, 40, 30)["features"][0]["covered_by"], [])
        self.assertTrue(result["assumptions"])

    def test_privacy_and_indoor_coverage(self):
        plan = self.plan(
            {"kind": "public_area", "name": "Sidewalk", "polygon": [[0, 26], [40, 26], [40, 30], [0, 30]]},
            {"kind": "camera", "name": "Drive", "point": [20, 20], "heading_deg": 180, "fov_deg": 90},
            {"kind": "room", "name": "Shop floor", "polygon": [[2, 2], [8, 2], [8, 8], [2, 8]]},
            {"kind": "camera", "name": "Till", "point": [2.5, 2.5], "heading_deg": 135, "fov_deg": 100, "mount": "indoor"},
        )
        result = planner.analyse_coverage(plan, 40, 30)
        self.assertEqual([p["camera"] for p in result["privacy"]], ["Drive"])
        self.assertIn("regulated", result["privacy"][0]["advice"])
        self.assertGreater(result["indoor_covered_pct"], 50)

    def test_installed_cameras_count_unless_already_on_the_plan(self):
        installed = [{"id": "cam_x", "name": "Existing", "map": {"x": 5, "y": 5, "heading_deg": 90, "fov_deg": 90}}]
        result = planner.analyse_coverage(self.plan(), 40, 30, installed)
        self.assertEqual([c["source"] for c in result["cameras"]], ["config"])
        linked = self.plan({"kind": "camera", "name": "Existing", "point": [5, 5], "camera_id": "cam_x", "status": "installed"})
        self.assertEqual(len(planner.analyse_coverage(linked, 40, 30, installed)["cameras"]), 1)

    def test_plan_validation(self):
        for bad in ({"items": [{"kind": "volcano"}]}, {"items": [{"kind": "building", "polygon": [[0, 0], [1, 1]]}]},
                    {"items": [{"kind": "camera", "point": [1e9, 0]}]}, {"items": [{"kind": "gate", "point": [1, 1], "id": "A B"}]}):
            with self.assertRaises(planner.PlanError):
                planner.clean_plan(bad)
        cleaned = planner.clean_plan({"items": [{"kind": "camera", "point": [1, 2], "heading_deg": -90, "secret": "x"}]})
        self.assertEqual(cleaned["items"][0]["heading_deg"], 270)
        self.assertNotIn("secret", cleaned["items"][0])

    def test_equipment_estimate(self):
        plan = self.plan(
            {"kind": "network", "name": "Switch", "point": [0, 0], "equipment": "switch", "poe_ports": 1},
            {"kind": "camera", "name": "Near", "point": [10, 0], "power": "poe"},
            {"kind": "camera", "name": "Far", "point": [95, 0], "power": "poe", "power_w": 6.5},
            {"kind": "camera", "name": "Battery", "point": [5, 5], "power": "battery"},
        )
        estimate = planner.estimate(plan)
        self.assertEqual(estimate["poe"]["ports_needed"], 2)
        self.assertEqual(estimate["poe"]["budget_w"], 21.9)
        self.assertEqual(estimate["poe"]["suggested_switch_ports"], 4)
        warnings = " ".join(estimate["warnings"])
        self.assertIn("Far", warnings)
        self.assertIn("1 PoE port(s) for 2", warnings)
        self.assertIn("battery-powered", warnings)

    def test_recommendation_and_report(self):
        result = planner.recommend({"property_type": "corner_lot", "exterior_doors": 2, "driveway": True, "garage_doors": 3, "gates": 1,
                                    "indoor_areas": 1, "want_sensors": True, "want_drone": True})
        roles = {line["role"]: line["count"] for line in result["lines"]}
        self.assertEqual(roles, {"Exterior doors": 2, "Driveway and garage": 2, "Gates": 1, "Indoor areas": 1})
        self.assertEqual(result["essential"], 4)
        self.assertTrue(any("corner lot" in note for note in result["notes"]))
        self.assertIn("not a guarantee", result["disclaimer"])
        plan = self.plan({"kind": "camera", "name": "<script>alert(1)</script>", "point": [5, 5]})
        report = planner.build_report(site_name="Maple <b>", path="new_cameras", property_info={}, plan=plan, width_m=40, height_m=30,
                                      installed=[], devices=[], recommendation=result)
        page = planner.render_html(report)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("not a guarantee", page)
        self.assertIn("PoE budget", page)


def _camera(camera_id, state="monitoring", connected=True, fresh=True, zones=1):
    now = int(time.time() * 1000)
    status = {"connected": connected, "last_frame_ms": now if fresh else 0,
              "health": {"state": state, "monitoring": state == "monitoring", "label": state}}
    return camera_entry({"id": camera_id, "name": camera_id, "source": {"type": "rtsp", "template": "hikvision"}, "zones": [{}] * zones}, status, now, running=True)


class ReadinessTests(unittest.TestCase):
    HEALTH = {"storage": {}, "database": {"ok": True}, "coverage": {"components": {}}}

    def assess(self, devices, path="existing_cameras", config=None, **state):
        return readiness(state={"path": path, "plan": {"items": []}, "steps": {}} | state, config=config or clean(CONFIG), devices=devices, health=self.HEALTH)

    def test_levels_are_kept_apart(self):
        devices = [_camera("a"), _camera("b", state="starting"), _camera("c", state="offline", connected=False, fresh=False)]
        self.assertEqual([d["level"] for d in devices], ["ai_verified", "video", "registered"])
        result = self.assess(devices)
        self.assertEqual(result["statement"]["state"], "partial")
        self.assertEqual(result["statement"]["text"], "AI monitoring verified on 1 of 3 camera(s).")
        self.assertEqual((result["levels"]["cameras_video"], result["levels"]["cameras_ai_verified"]), (2, 1))
        for text in json.dumps(result).lower().split('"'):
            for word in FORBIDDEN:
                self.assertNotIn(word, text)

    def test_no_verified_camera_means_not_monitoring(self):
        result = self.assess([_camera("a", state="frozen")])
        self.assertEqual(result["statement"]["state"], "not_monitoring")
        self.assertTrue(any(c["status"] == "fail" and c["step"] == "verify" for c in result["checks"]))

    def test_blocking_conditions(self):
        self.assertFalse(self.assess([_camera("a")], path="")["can_complete"])
        result = self.assess([], path="existing_cameras")
        self.assertFalse(result["can_complete"])
        self.assertIn("Register at least a camera", result["blocking"])
        self.assertTrue(self.assess([_camera("a")])["can_complete"])

    def test_drone_first_without_cameras(self):
        drone = device_entry({"id": "d", "category": "drone", "name": "Drone", "profile": "simulated_drone", "simulated": True},
                             {"telemetry": {"connected": True, "link_ok": True}, "preflight_blockers": [], "primary": True}, {})
        planned = device_entry({"id": "s", "category": "contact_sensor", "name": "Door", "profile": "mqtt_sensor"}, None, {})
        self.assertEqual((planned["level"], planned["state"]), ("registered", "not_connected"))
        result = self.assess([drone, planned], path="drone_first", config=clean(DRONE_FIRST))
        self.assertTrue(result["can_complete"])
        self.assertEqual(result["statement"]["state"], "no_cameras")
        self.assertIn("approval", " ".join(result["statement"]["extras"]))
        self.assertTrue(any(c["id"] == "planned_s" for c in result["checks"]))
        self.assertTrue(any(c["id"] == "simulated" for c in result["checks"]))


class InstallationStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def test_save_and_resume(self):
        manager = InstallationManager(self.directory.name)
        manager.update({"path": "new_cameras", "current_step": "plan", "steps": {"property": "done"}, "property": {"property_type": "storefront"}})
        manager.set_plan({"items": [{"kind": "camera", "name": "Door", "point": [1, 1], "camera_id": "cam_a", "status": "installed"}]})
        resumed = InstallationManager(self.directory.name).state
        self.assertEqual((resumed["path"], resumed["current_step"]), ("new_cameras", "plan"))
        self.assertEqual(resumed["steps"]["property"]["status"], "done")
        self.assertEqual(len(resumed["plan"]["items"]), 1)
        self.assertEqual(manager.unlink_device("cam_a"), 1)
        self.assertEqual(manager.state["plan"]["items"][0]["status"], "proposed")
        manager.reset(keep_plan=True)
        self.assertEqual((manager.state["path"], len(manager.state["plan"]["items"])), ("", 1))
        with self.assertRaises(ValueError):
            manager.update({"steps": {"finish": "done"}})

    def test_damaged_progress_is_preserved_not_overwritten(self):
        path = Path(self.directory.name) / "installation.json"
        path.write_text("{not json", encoding="utf-8")
        manager = InstallationManager(self.directory.name)
        self.assertIn("recovery/", manager.recovered)
        kept = list((Path(self.directory.name) / "recovery").glob("installation.*.damaged.json"))
        self.assertEqual(kept[0].read_text(encoding="utf-8"), "{not json")


class FakeNtfy(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        FakeNtfy.received.append((self.path, self.rfile.read(int(self.headers["Content-Length"])).decode()))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@unittest.skipIf(TestClient is None, "fastapi not installed")
class InstallationServiceTests(unittest.TestCase):
    def make(self, base, *, start_cameras=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = copy.deepcopy(base)  # keeps _config_dir, so simulated scenarios resolve
        config["storage"] = {"dir": directory.name}
        service = SentinelService(config, start_cameras=start_cameras)
        service.start()
        self.addCleanup(service.stop)
        return service, TestClient(create_app(service, api_token=None))

    def test_drone_first_site_without_cameras(self):
        service, client = self.make(DRONE_FIRST)
        self.assertEqual(client.get("/api/v1/state").json()["coverage"]["state"], "no_cameras")
        devices = {d["id"]: d for d in client.get("/api/v1/devices").json()["devices"]}
        self.assertEqual(devices["yard_drone"]["state"], "ready")
        self.assertTrue(devices["yard_drone"]["primary"])
        self.assertEqual(devices["front_door_contact"]["state"], "reporting")
        self.assertEqual(devices["yard_dock"]["detail"], "Drone docked")
        self.assertEqual(devices["hub_local"]["state"], "running")

        event = client.post("/api/v1/devices/front_door_contact/test").json()
        self.assertEqual((event["event_type"], event["severity"]), ("sensor_test", "info"))
        self.assertEqual(client.get("/api/v1/incidents").json(), [], "a sensor test must never open an incident")

        site = client.get("/api/v1/site").json()
        self.assertTrue(site["drone_enabled"])
        self.assertEqual([p["id"] for p in site["drone_points"]], ["back_fence", "driveway"])
        request = client.post("/api/v1/drone/inspections", json={"point_id": "driveway"}).json()
        self.assertEqual((request["status"], request["target_label"]), ("pending_approval", "Driveway"))
        self.assertEqual(client.post("/api/v1/drone/inspections", json={"point_id": "nowhere"}).status_code, 404)

        client.put("/api/v1/installation", json={"path": "drone_first"})
        view = client.get("/api/v1/installation").json()
        self.assertTrue(view["readiness"]["can_complete"])
        self.assertEqual(view["readiness"]["statement"]["state"], "no_cameras")
        self.assertEqual(client.post("/api/v1/installation/complete", json={}).status_code, 409, "open issues need acknowledging")
        done = client.post("/api/v1/installation/complete", json={"acknowledge_issues": True}).json()
        self.assertTrue(done["readiness"]["levels"]["installation_completed"])
        self.assertTrue(done["state"]["completed_with_issues"])

        # Removing the only drone turns drone features off rather than inventing one.
        self.assertEqual(client.delete("/api/v1/devices/yard_drone").status_code, 200)
        self.assertEqual(client.get("/api/v1/drone").status_code, 404)
        self.assertIsNone(service.drone)

    def test_planned_hardware_registers_but_never_connects(self):
        service, client = self.make(DRONE_FIRST)
        created = client.post("/api/v1/devices", json={"category": "drone", "name": "Future drone", "profile": "sentinel_drone_outdoor"}).json()
        entry = next(d for d in client.get("/api/v1/devices").json()["devices"] if d["id"] == created["id"])
        self.assertEqual(entry["support"], "planned")
        # The simulated drone keeps working; the planned one has no adapter, so it is never connected or ready.
        self.assertEqual((entry["level"], entry["state"]), ("registered", "not_connected"))
        self.assertNotIn(created["id"], service.drones)
        self.assertEqual(client.post("/api/v1/devices", json={"category": "recorder", "name": "x", "profile": "cloud_only_camera"}).status_code, 422)

    def test_drone_changes_wait_until_the_drone_has_landed(self):
        service, client = self.make(DRONE_FIRST)
        manager = service.drones["yard_drone"]
        client.post("/api/v1/drone/commands", json={"action": "takeoff"})
        wait_for(lambda: manager.adapter.telemetry().in_air, message="simulated drone did not take off")
        client.put("/api/v1/devices/yard_drone", json={"name": "Renamed while flying"})
        self.assertIs(service.drones["yard_drone"], manager, "managers must not be rebuilt mid-flight")
        self.assertIsNotNone(service._drone_rebuild_pending)
        service._drone_rebuild_pending = None  # stop the loop from applying it while the test ends

    def test_existing_cameras_then_expand_and_replace(self):
        service, client = self.make(CONFIG)
        client.put("/api/v1/installation", json={"path": "existing_cameras"})
        client.post("/api/v1/installation/property", json={"name": "Maple Street", "property_type": "house", "width_m": 40, "height_m": 30})
        self.assertEqual(service.site.config["site"]["name"], "Maple Street")
        plan = client.put("/api/v1/installation/plan", json={"items": [
            {"kind": "camera", "name": "Front", "point": [20, 25], "camera_id": "cam_front", "status": "installed"},
            {"kind": "entrance", "name": "Door", "point": [20, 20]}]}).json()
        self.assertIn("coverage", plan)
        recorder = client.post("/api/v1/devices", json={"category": "recorder", "name": "NVR", "profile": "hikvision_recorder", "host": "10.0.0.84"}).json()
        self.assertEqual(next(d for d in client.get("/api/v1/devices").json()["devices"] if d["id"] == recorder["id"])["state"], "no_channels")
        # Later expansion: a sensor, then replacing a camera.
        client.post("/api/v1/devices", json={"category": "motion_sensor", "name": "Hall", "simulated": True})
        removed = client.delete("/api/v1/devices/cam_front").json()
        self.assertEqual(removed["plan_items_unlinked"], 1)
        self.assertEqual(service.installation.state["plan"]["items"][0]["status"], "proposed")
        ids = [d["id"] for d in client.get("/api/v1/devices").json()["devices"]]
        self.assertNotIn("cam_front", ids)
        self.assertIn("hall", ids)
        report = client.get("/api/v1/installation/report").json()
        self.assertIn("installation", report["checklists"])
        html = client.get("/api/v1/installation/report.html")
        self.assertEqual(html.status_code, 200)
        self.assertIn("default-src 'none'", html.headers["content-security-policy"])

    def test_test_notification_goes_through_the_stored_topic(self):
        server = HTTPServer(("127.0.0.1", 0), FakeNtfy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        service, client = self.make(CONFIG)
        client.put("/api/v1/config/notifications", json={"ntfy": {"enabled": True, "server": f"http://127.0.0.1:{server.server_port}", "topic": "maple-test-topic"}})
        FakeNtfy.received.clear()
        result = client.post("/api/v1/installation/test-notification").json()
        self.assertEqual(result["outcomes"]["ntfy"], "sent")
        self.assertEqual(FakeNtfy.received[0][0], "/maple-test-topic")
        self.assertIn("No activity was observed", FakeNtfy.received[0][1])
        checks = {c["id"]: c for c in client.get("/api/v1/installation").json()["readiness"]["checks"]}
        self.assertEqual(checks["alert_test"]["status"], "pass")
        # An unreachable push server is reported as a failure, not as success.
        client.put("/api/v1/config/notifications", json={"ntfy": {"enabled": True, "server": "http://127.0.0.1:9"}})
        failed = client.post("/api/v1/installation/test-notification").json()
        self.assertTrue(failed["outcomes"]["ntfy"].startswith("failed"))
        checks = {c["id"]: c for c in client.get("/api/v1/installation").json()["readiness"]["checks"]}
        self.assertEqual(checks["alert_test"]["status"], "fail")

    def test_backups_carry_installation_progress(self):
        service, client = self.make(CONFIG)
        client.put("/api/v1/installation", json={"path": "complete_package"})
        client.put("/api/v1/installation/plan", json={"items": [{"kind": "drone_dock", "name": "Dock", "point": [3, 3]}]})
        name = client.post("/api/v1/system/backups", json={"label": "manual"}).json()["name"]
        client.post("/api/v1/installation/reset", json={"keep_plan": False})
        self.assertEqual(service.installation.state["plan"]["items"], [])
        client.post(f"/api/v1/system/backups/{name}/restore", json={"parts": ["config"]})
        state = service.installation.state
        self.assertEqual((state["path"], len(state["plan"]["items"])), ("complete_package", 1))

    def test_simulated_cameras_reach_ai_verified(self):
        """With processing running, the scripted detector on simulated cameras
        earns each level from real frames: nothing is assumed from registration."""
        service, client = self.make(CONFIG, start_cameras=True)

        def verified():
            levels = client.get("/api/v1/installation").json()["readiness"]["levels"]
            return levels["cameras_ai_verified"] == levels["cameras_total"] == 3

        wait_for(verified, timeout=45, message="simulated cameras never reached AI monitoring verified")
        statement = client.get("/api/v1/installation").json()["readiness"]["statement"]
        self.assertEqual(statement["state"], "monitoring_verified")
        self.assertIn("simulated", " ".join(statement["extras"]))


if __name__ == "__main__":
    unittest.main()
