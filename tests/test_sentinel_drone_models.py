"""Drone models (Sentinel Lite, Pro, Thermal): the capability catalog, model-specific
readiness and safety checks, video settings, and simulated inspections of every
model through the same workflow. No hardware; physical aircraft stay telemetry-only."""

from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

import sentinel.drone.manager as manager_module
from sentinel.config import load_config, validate_config
from sentinel.drone import DroneManager, FlightCommandRejected, build_drone_manager, catalog
from sentinel.drone.base import map_point_to_geo
from sentinel.drone.manager import DroneSafetyConfig
from sentinel.drone.troubleshoot import diagnose
from sentinel.store import EventStore

from test_sentinel_drone_v1 import FENCE, GOOD_PARAMS, HOME, ORIGIN, SHED, FakeArduPilot, FakeClock

try:
    from fastapi.testclient import TestClient

    from sentinel.api import create_app
    from sentinel.service import SentinelService
except ImportError:  # FastAPI is only needed for the service-level tests
    TestClient = None

DRONE_FIRST = load_config("config/sentinel.drone-first.example.json", resolve_secrets=False)
LOCKOUT = {"MAV_OPTIONS": 1, "MAV_GCS_SYSID": 255}
REPORTING_COMPANION = {"name": "cm4", "state": "reporting", "report": {"relay": {"policy": "telemetry"}}}


def daylight(manager, **extra):
    manager.set_conditions("op", precipitation=False, visibility_ok=True, wind_mps=3, temperature_c=12, daylight=True, **extra)


class CatalogTests(unittest.TestCase):
    def test_every_model_is_complete_and_honest(self):
        for model in catalog.MODELS:
            with self.subTest(model=model["id"]):
                self.assertEqual(set(model["capabilities"]), set(catalog.CAPABILITIES))
                self.assertTrue(set(model["capabilities"].values()) <= set(catalog.CAPABILITY_VALUES))
                for key in ("hfov_deg", "vfov_deg", "default_pitch_deg", "source"):
                    self.assertIn(key, model["camera"])
                for key in ("endurance_min_est", "cruise_mps", "climb_mps", "max_wind_mps", "night_operations", "source"):
                    self.assertIn(key, model["flight"])
                self.assertTrue(model["limitations"], "every model states its limits")
                for rule in model["parameters"]:
                    self.assertIn(rule["test"], ("eq", "not", "min", "bit"))
                    self.assertTrue(rule["why"])
                    if rule.get("option"):
                        self.assertIn(rule["option"], model["options"])
        for model_id in catalog.TIERS:
            model = catalog.MODEL_INDEX[model_id]
            self.assertEqual(model["status"], "reference_design", "no Sentinel drone has been built or flown")
            self.assertEqual(model["capabilities"]["command_lockout"], "yes")
            self.assertEqual(model["capabilities"]["fc_failsafes"], "yes")
        text = json.dumps(catalog.MODELS).lower()
        for word in ("$", "usd", "cad", "price"):
            self.assertNotIn(word, text, "parts estimates live in docs/drone-models.md, never in the catalog")
        for word in ("protected", "secure", "guarantee"):
            self.assertNotIn(word, text)

    def test_cheapest_model_carries_only_what_the_job_needs(self):
        lite = catalog.MODEL_INDEX["sentinel_lite"]
        for capability in ("thermal", "obstacle_sensing", "optical_zoom"):
            self.assertEqual(lite["capabilities"][capability], "no")
        self.assertEqual(lite["requires"]["companion"], "none")
        self.assertEqual(lite["capabilities"]["companion_computer"], "optional")
        for capability in ("telemetry", "gps", "battery_monitoring", "manual_control", "fc_failsafes", "video_stream", "stabilized_video"):
            self.assertEqual(lite["capabilities"][capability], "yes", capability)
        costs = [catalog.MODEL_INDEX[m]["cost_rank"] for m in catalog.TIERS]
        self.assertEqual(costs, sorted(costs))

    def test_comparison_and_recommendation(self):
        table = catalog.comparison()
        self.assertEqual([m["id"] for m in table["models"]], list(catalog.TIERS))
        self.assertEqual(len(table["rows"]), len(catalog.CAPABILITIES))
        self.assertTrue(table["shared"])
        self.assertIn("nothing has flown", table["support_note"])
        self.assertEqual(catalog.recommend({})["model"], "sentinel_lite")
        self.assertEqual(catalog.recommend({"night": True})["model"], "sentinel_thermal")
        self.assertEqual(catalog.recommend({"windy": True})["model"], "sentinel_pro")
        self.assertEqual(catalog.recommend({"property_size_m": 200})["model"], "sentinel_pro")
        cheap_night = catalog.recommend({"night": True, "budget": "lowest"})
        self.assertTrue(any("daytime" in caution for caution in cheap_night["cautions"]))

    def test_simulator_and_video_settings_follow_the_model(self):
        lite, pro, thermal = (catalog.MODEL_INDEX[m] for m in catalog.TIERS)
        self.assertLess(catalog.simulator_settings(lite)["cruise_speed_mps"], catalog.simulator_settings(pro)["cruise_speed_mps"])
        self.assertGreater(catalog.simulator_settings(lite)["drain_pct_per_s"], catalog.simulator_settings(pro)["drain_pct_per_s"])
        self.assertEqual(catalog.simulator_settings(lite)["gimbal_pitch_deg"], -90)
        self.assertNotIn("drain_pct_per_s", catalog.simulator_settings(catalog.get_model(None)), "unknown endurance keeps the default")

        self.assertIsNone(catalog.video_settings(lite, {}, simulated=False), "never guess a real camera's address")
        explicit = catalog.video_settings(pro, {"video": {"source": {"type": "rtsp", "url": "rtsp://192.168.144.25:8554/main.264"}}}, simulated=False)
        self.assertEqual(explicit["camera"]["hfov_deg"], pro["camera"]["hfov_deg"])
        site_video = {"source": {"type": "simulated_aerial", "objects": [{"label": "person", "path": [{"t": 0, "map": [1, 1]}]}]}}
        simulated = catalog.video_settings(thermal, {}, simulated=True, site_video=site_video)
        self.assertEqual(simulated["source"]["type"], "simulated_aerial")
        self.assertEqual(len(simulated["source"]["objects"]), 1)
        self.assertTrue(any("Thermal channel not analysed" in note for note in simulated["notes"]))
        self.assertEqual(simulated["model"], "sentinel_thermal")

    def test_endurance_check(self):
        lite = catalog.MODEL_INDEX["sentinel_lite"]
        route = {"distance_m": 2 * 30 + 2 * 12, "transit_alt_m": 12, "hover_s": 20}
        self.assertAlmostEqual(catalog.estimate_flight_s(lite, route), 12 + 12 + 20 + 30)
        self.assertTrue(catalog.endurance_check(lite, route, 90, 30)["ok"])
        short = catalog.endurance_check(lite, route, 36, 30)
        self.assertFalse(short["ok"])
        self.assertIn("reserve", short["detail"])
        self.assertFalse(catalog.endurance_check(catalog.get_model(None), route, 31, 30)["checked"])

    def test_command_lockout(self):
        lite, generic = catalog.MODEL_INDEX["sentinel_lite"], catalog.get_model(None)
        self.assertEqual(catalog.command_lockout(lite, GOOD_PARAMS | LOCKOUT, 245, None)[0], "pass")
        self.assertEqual(catalog.command_lockout(lite, GOOD_PARAMS | {"SYSID_ENFORCE": 1, "SYSID_MYGCS": 255}, 245, None)[0], "pass")
        state, detail = catalog.command_lockout(lite, GOOD_PARAMS | {"MAV_OPTIONS": 1, "MAV_GCS_SYSID": 245}, 245, None)
        self.assertEqual(state, "fail")
        self.assertIn("is the Hub", detail)
        self.assertEqual(catalog.command_lockout(lite, GOOD_PARAMS, 245, None)[0], "fail")
        self.assertEqual(catalog.command_lockout(generic, GOOD_PARAMS, 245, None)[0], "warn")
        self.assertEqual(catalog.command_lockout(lite, {}, 245, REPORTING_COMPANION)[0], "pass")
        sitl_relay = REPORTING_COMPANION | {"report": {"relay": {"policy": "sitl"}}}
        self.assertEqual(catalog.command_lockout(lite, GOOD_PARAMS, 245, sitl_relay)[0], "fail", "a pass-through relay is no lockout")

    def test_device_validation(self):
        config = copy.deepcopy({k: v for k, v in DRONE_FIRST.items() if not k.startswith("_")})
        drone = config["devices"][0]
        drone["drone_model"] = "sentinel_pro"
        drone["drone_options"] = {"obstacle_sensor": True}
        validate_config(config)
        drone["drone_options"] = {"thermal": True}
        with self.assertRaisesRegex(Exception, "not an option"):
            validate_config(config)
        drone["drone_options"] = {}
        drone["drone_model"] = "dji_mini"
        with self.assertRaisesRegex(Exception, "drone_model must be one of"):
            validate_config(config)


class ModelReadinessTests(unittest.TestCase):
    """Physical aircraft (telemetry only) of each model, with a fake ArduPilot."""

    def setUp(self):
        self.real_time = manager_module.time
        self.clock = FakeClock()
        manager_module.time = self.clock
        self.directory = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.directory.name) / "sentinel.db")
        self.adapter = FakeArduPilot()

    def tearDown(self):
        manager_module.time = self.real_time
        self.store.close()
        self.directory.cleanup()

    def manager(self, model_id, options=None, **safety):
        model = catalog.MODEL_INDEX[model_id]
        manager = DroneManager(self.adapter, FENCE, ORIGIN, DroneSafetyConfig(**({"inspection_altitude_m": 12, "hover_s": 5} | safety)),
                               drone_id="d1", home=HOME, records=self.store, model=model, options=options or {},
                               weather=manager_module.WeatherLimits.from_dict(catalog.weather_defaults(model)))
        manager.video_status = lambda: {"state": "idle", "detail": "waiting for an approved inspection"}
        return manager

    def test_lite_needs_an_independent_command_lockout_and_a_camera_stream(self):
        manager = self.manager("sentinel_lite")
        manager.video_status = lambda: None
        daylight(manager)
        blockers = manager.readiness()["blockers"]
        self.assertIn("no_command_lockout", blockers)
        self.assertIn("no_drone_video", blockers)
        companion = next(c for c in manager.readiness()["checks"] if c["id"] == "companion")
        self.assertEqual(companion["state"], "na", "the Lite has no companion computer")
        self.adapter.params |= LOCKOUT | {"BATT_MONITOR": 4}
        manager.video_status = lambda: {"state": "idle", "detail": "waiting"}
        self.assertTrue(manager.readiness()["ready"], manager.readiness()["blockers"])
        audit = manager.parameter_audit()
        gimbal = next(f for f in audit["findings"] if f["param"] == "MNT1_TYPE")
        self.assertEqual((gimbal["ok"], gimbal["blocking"], gimbal["model"]), (False, False, "Sentinel Lite"))
        self.adapter.params["BATT_MONITOR"] = 0
        self.assertIn("flight_controller_settings", manager.readiness()["blockers"])

    def test_day_only_models_refuse_night_and_thermal_needs_lights(self):
        self.adapter.params |= LOCKOUT | {"BATT_MONITOR": 4, "PRX1_TYPE": 8}
        lite = self.manager("sentinel_lite")
        lite.set_conditions("op", precipitation=False, visibility_ok=True, daylight=False)
        self.assertIn("camera_cannot_see_in_the_dark", lite.readiness()["blockers"])
        lite.set_conditions("op", precipitation=False, visibility_ok=True)
        check = next(c for c in lite.readiness()["checks"] if c["id"] == "daylight")
        self.assertEqual(check["state"], "warn", "a daytime aircraft asks for daylight to be confirmed")

        thermal = self.manager("sentinel_thermal")
        thermal.companion_status = lambda: REPORTING_COMPANION
        thermal.set_conditions("op", precipitation=False, visibility_ok=True, daylight=False)
        self.assertIn("night_lights_not_confirmed", thermal.readiness()["blockers"])
        thermal.set_conditions("op", precipitation=False, visibility_ok=True, daylight=False, lights_ok=True)
        self.assertTrue(thermal.readiness()["ready"], thermal.readiness()["blockers"])

    def test_wind_and_temperature_limits_come_from_the_model(self):
        self.adapter.params |= LOCKOUT | {"BATT_MONITOR": 4}
        lite = self.manager("sentinel_lite")
        daylight(lite)
        lite.set_conditions("op", precipitation=False, visibility_ok=True, wind_mps=7, daylight=True)
        self.assertIn("conditions_outside_limits", lite.readiness()["blockers"], "7 m/s is over the Lite's 6 m/s")
        pro = self.manager("sentinel_pro")
        pro.companion_status = lambda: REPORTING_COMPANION
        pro.set_conditions("op", precipitation=False, visibility_ok=True, wind_mps=7, daylight=True)
        self.assertNotIn("conditions_outside_limits", pro.readiness()["blockers"])
        lite.set_conditions("op", precipitation=False, visibility_ok=True, temperature_c=-3, daylight=True)
        self.assertIn("conditions_outside_limits", lite.readiness()["blockers"], "LiPo packs and the Lite stop at 0 °C")

    def test_pro_requires_its_companion_and_checks_optional_lidar(self):
        self.adapter.params |= {"BATT_MONITOR": 4}
        pro = self.manager("sentinel_pro", options={"obstacle_sensor": True})
        daylight(pro)
        blockers = pro.readiness()["blockers"]
        self.assertIn("companion_not_paired", blockers)
        self.assertIn("no_command_lockout", blockers)
        self.assertIn("flight_controller_settings", blockers, "the lidar is installed but PRX1_TYPE is not set")
        pro.companion_status = lambda: REPORTING_COMPANION
        self.adapter.params["PRX1_TYPE"] = 8
        self.assertTrue(pro.readiness()["ready"], pro.readiness()["blockers"])
        without_lidar = self.manager("sentinel_pro")
        self.assertNotIn("PRX1_TYPE", [f["param"] for f in without_lidar.parameter_audit()["findings"]])

    def test_routes_that_exceed_the_battery_are_refused(self):
        self.adapter.params |= LOCKOUT | {"BATT_MONITOR": 4}
        manager = self.manager("sentinel_lite", min_launch_battery_pct=35, hover_s=20)
        daylight(manager)
        self.adapter.t.battery_pct = 36
        request = manager.request_inspection(requested_by="op", lat=SHED[0], lon=SHED[1], label="Shed")
        self.assertEqual(request.aircraft, "Sentinel Lite (aircraft, telemetry only)")
        with self.assertRaisesRegex(FlightCommandRejected, "route_exceeds_endurance"):
            manager.approve(request.request_id, "op")
        self.adapter.t.battery_pct = 90
        manager.approve(request.request_id, "op")
        self.assertTrue(request.flight_estimate["ok"])
        self.assertEqual(request.mode, "propose", "physical aircraft of every model are pilot-flown")
        self.assertTrue(any(note.startswith("Endurance:") for note in request.route["notes"]))

    def test_diagnosis_explains_each_blocker_for_the_model(self):
        manager = self.manager("sentinel_lite")
        manager.video_status = lambda: None
        self.adapter.t.link_ok = False
        report = diagnose(manager.status(), manager.model)
        checks = [f["check"] for f in report["findings"]]
        self.assertEqual(checks[0], "link", "a lost link is explained first")
        for expected in ("lockout", "video", "conditions"):
            self.assertIn(expected, checks)
        link = report["findings"][0]
        self.assertTrue(any("HM30" in step for step in link["steps"]))
        lockout = next(f for f in report["findings"] if f["check"] == "lockout")
        self.assertTrue(any("MAV_OPTIONS" in step for step in lockout["steps"]))
        video = next(f for f in report["findings"] if f["check"] == "video")
        self.assertTrue(any("rtsp://192.168.144.25" in step for step in video["steps"]))
        self.assertIn("block approval", report["summary"])


def model_config(model_id: str, **drone_extra):
    config = copy.deepcopy(DRONE_FIRST)
    config["devices"][0] |= {"drone_model": model_id}
    config["drone"]["simulator"] = {}
    config["drone"]["safety"] |= {"inspection_altitude_m": 10, "hover_s": 4}
    config["drone"] |= drone_extra
    return config


class ModelSimulationTests(unittest.TestCase):
    """Every model through the same incident -> approval -> flight -> video ->
    evidence workflow, in the built-in simulator (simulated time runs fast)."""

    def setUp(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV not installed")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EventStore(Path(self.directory.name) / "sentinel.db")
        self.addCleanup(self.store.close)

    def build(self, model_id: str):
        from sentinel.drone.video import DroneVideoWorker

        target = DRONE_FIRST["drone"]["inspection_points"][1]["map"]  # driveway
        site_video = {"source": {"type": "simulated_aerial", "objects": [{"label": "person", "path": [{"t": 0, "map": [target[0] - 0.5, target[1]]}]}]},
                      "fps": 10}
        config = model_config(model_id, video=site_video)
        device = config["devices"][0]
        manager = build_drone_manager(config, audit=self.store.audit, on_change=lambda status: None, device=device, records=self.store)
        settings = catalog.video_settings(manager.model, device, simulated=True, site_video=site_video) | {"fps": 10}
        worker = DroneVideoWorker(manager, settings, config={}, media_root=Path(self.directory.name) / "media", record_evidence=self.store.record_evidence)
        manager.video_status = worker.status
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(worker.stop)
        return manager, worker, tuple(target)

    def fly(self, manager, request, until=("completed", "aborted"), timeout=40, on_step=None):
        simulated_s = 0.0
        deadline = time.time() + timeout
        while request.status not in until and time.time() < deadline:
            manager.step(0.25)
            simulated_s += 0.25
            if on_step:
                on_step(simulated_s)
            time.sleep(0.01)
        return simulated_s

    def test_every_model_completes_the_same_inspection_workflow(self):
        arrivals = {}  # simulated seconds from approval to the inspection point: kinematics only (the hover runs on the wall clock)
        for model_id in catalog.TIERS:
            with self.subTest(model=model_id):
                manager, worker, target = self.build(model_id)
                self.assertEqual(manager.mode()[0], "command", "the built-in simulator is the only thing Sentinel flies")
                status = manager.status()
                self.assertEqual(status["model"]["id"], model_id)
                self.assertTrue(manager.readiness()["ready"], manager.readiness()["blockers"])
                request = manager.request_inspection(requested_by="sentinel", map_point=target, label="Driveway", source="incident",
                                                     incident_id=f"inc_{model_id}", reason="Person entered Driveway")
                self.assertEqual(request.status, "pending_approval", "requests from incidents always wait for a person")
                self.assertIn("simulated", request.aircraft)
                manager.approve(request.request_id, "operator")

                def arrived(simulated_s, request=request, model_id=model_id):
                    if request.phase == "inspecting" and model_id not in arrivals:
                        arrivals[model_id] = simulated_s

                self.fly(manager, request, on_step=arrived)
                self.assertEqual(request.status, "completed", request.note)
                deadline = time.time() + 5
                while not request.observations.get("final") and time.time() < deadline:
                    time.sleep(0.05)
                summary = request.observations
                self.assertEqual(summary.get("model"), model_id)
                self.assertEqual(summary["max_visible"], {"person": 1})
                self.assertTrue(request.evidence_paths)
                self.assertEqual(summary["pose_sources"], ["gimbal_reported"], "the model's gimbal reports its angle")
                self.assertTrue(summary["text"][-1].startswith("Sentinel reports observed activity"), "disclaimer stays last")
                if model_id == "sentinel_thermal":
                    self.assertTrue(any("Thermal channel not analysed" in line for line in summary["text"]))
                stored = self.store.list_drone_inspections(incident_id=f"inc_{model_id}")[0]
                self.assertEqual((stored["status"], stored["aircraft"]), ("completed", request.aircraft))
                worker.stop()
                worker.join(5)
        self.assertGreater(arrivals["sentinel_lite"], arrivals["sentinel_pro"], "the Lite flies slower than the Pro")

    def test_link_loss_and_low_battery_return_home_for_every_model(self):
        for model_id in catalog.TIERS:
            with self.subTest(model=model_id, fault="link_loss"):
                manager, worker, target = self.build(model_id)
                request = manager.request_inspection(requested_by="op", map_point=target, label="Driveway")
                manager.approve(request.request_id, "op")
                lost = {}

                def cut_link(_, manager=manager, request=request, lost=lost):
                    if request.phase == "enroute" and not lost:
                        manager.adapter.set_link(False)
                        lost["at"] = True

                self.fly(manager, request, until=("aborted", "completed"), on_step=cut_link)
                self.assertEqual(request.status, "aborted")
                self.assertIn("returned", request.note)
                self.assertIn("drone_link_lost", [row["action"] for row in self.store.list_audit(500)])
                manager.adapter.set_link(True)
                worker.stop()
                worker.join(5)
            with self.subTest(model=model_id, fault="low_battery"):
                manager, worker, target = self.build(model_id)
                request = manager.request_inspection(requested_by="op", map_point=target, label="Driveway")
                manager.approve(request.request_id, "op")

                def drain(_, manager=manager, request=request):
                    if request.phase == "enroute" and manager.adapter.battery > 29:
                        manager.adapter.battery = 29.0

                self.fly(manager, request, until=("aborted", "completed"), on_step=drain)
                self.assertEqual(request.status, "aborted")
                self.assertEqual(manager.adapter.mode, "docked", "returned home and landed")
                self.assertIn("drone_auto_return_home", [row["action"] for row in self.store.list_audit(500)])
                worker.stop()
                worker.join(5)

    def test_lite_refuses_a_night_inspection_that_thermal_can_fly(self):
        lite, _, target = self.build("sentinel_lite")
        lite.set_conditions("op", precipitation=False, visibility_ok=True, daylight=False)
        request = lite.request_inspection(requested_by="op", map_point=target, label="Driveway")
        with self.assertRaisesRegex(FlightCommandRejected, "camera_cannot_see_in_the_dark"):
            lite.approve(request.request_id, "op")
        thermal, _, _ = self.build("sentinel_thermal")
        thermal.set_conditions("op", precipitation=False, visibility_ok=True, daylight=False, lights_ok=True)
        request = thermal.request_inspection(requested_by="op", map_point=target, label="Driveway")
        thermal.approve(request.request_id, "op")
        self.assertEqual(request.status, "in_progress")
        thermal.command("land", "op")


@unittest.skipIf(TestClient is None, "FastAPI not installed")
class LiteServiceTests(unittest.TestCase):
    """Sentinel Lite end to end through the real service and API (built-in simulator, real time)."""

    def make(self, config):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config["storage"] = {"dir": directory.name}
        service = SentinelService(config, start_cameras=False)
        service.start()
        self.addCleanup(service.stop)
        return service, TestClient(create_app(service, api_token=None))

    def test_registration_comparison_inspection_and_diagnosis(self):
        target = DRONE_FIRST["drone"]["inspection_points"][0]["map"]  # back fence, near the drone's home
        video = {"source": {"type": "simulated_aerial", "objects": [{"label": "person", "path": [{"t": 0, "map": [target[0], target[1] + 0.5]}]}]}}
        config = model_config("sentinel_lite", video=video)
        config["drone"]["safety"] |= {"inspection_altitude_m": 6, "hover_s": 3}
        config["drone"]["geofence"]["min_altitude_m"] = 3
        service, client = self.make(config)

        models = client.get("/api/v1/drone/models").json()
        self.assertEqual([m["id"] for m in models["models"]], list(catalog.TIERS))
        self.assertEqual(client.post("/api/v1/drone/models/recommend", json={"night": True}).json()["model"], "sentinel_thermal")

        status = client.get("/api/v1/drone").json()
        self.assertEqual(status["model"]["name"], "Sentinel Lite")
        device = next(d for d in client.get("/api/v1/devices").json()["devices"] if d["id"] == "yard_drone")
        self.assertEqual((device["drone_model"], device["model_name"]), ("sentinel_lite", "Sentinel Lite"))
        deadline = time.time() + 10  # the camera is "unverified" until the first pre-flight stream check has seen a live picture
        while (client.get("/api/v1/drone").json().get("video") or {}).get("state") != "ready" and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(client.get("/api/v1/drone/diagnosis").json()["summary"],
                         "All readiness checks pass. Every flight still needs an operator's approval.")

        request = client.post("/api/v1/drone/inspections", json={"point_id": "back_fence"}).json()
        self.assertEqual(request["aircraft"], "Sentinel Lite (simulated)")
        approved = client.post(f"/api/v1/drone/inspections/{request['request_id']}/approve").json()
        self.assertTrue(approved["flight_estimate"]["ok"])
        deadline = time.time() + 60
        final = {}
        while time.time() < deadline:
            final = client.get(f"/api/v1/drone/inspections/{request['request_id']}").json()
            if final["status"] in ("completed", "aborted") and final["observations"].get("final"):
                break
            time.sleep(0.5)
        self.assertEqual(final["status"], "completed", final.get("note"))
        self.assertEqual(final["observations"]["model"], "sentinel_lite")
        self.assertEqual(final["observations"]["max_visible"], {"person": 1})

        # Registering more models: options are checked, stream addresses are write-only.
        pro = client.post("/api/v1/devices", json={"category": "drone", "name": "Pro on the bench", "profile": "mavlink_drone", "drone_model": "sentinel_pro",
                                                   "connection": "udpin:0.0.0.0:14552", "drone_options": {"obstacle_sensor": True},
                                                   "video_url": "rtsp://192.168.144.25:8554/main.264"})
        self.assertEqual(pro.status_code, 200, pro.text)
        self.assertNotIn("192.168.144.25", pro.text)
        self.assertTrue(pro.json()["video"]["source"]["url_set"])
        self.assertNotIn("192.168.144.25", client.get("/api/v1/config").text)
        bad = client.post("/api/v1/devices", json={"category": "drone", "name": "x", "profile": "simulated_drone", "simulated": True,
                                                   "drone_model": "sentinel_lite", "drone_options": {"obstacle_sensor": True}})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(client.get("/api/v1/drone/diagnosis", params={"drone_id": "nope"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
