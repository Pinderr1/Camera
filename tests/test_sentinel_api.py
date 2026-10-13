import copy
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:  # API deps are optional for the core engine
    TestClient = None

from sentinel.config import load_config

CONFIG = load_config("config/sentinel.example.json", resolve_secrets=False)
TOKEN = "test-token-123"


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        config = copy.deepcopy(CONFIG)
        config["storage"] = {"dir": self.directory.name}
        self.service = SentinelService(config, start_cameras=False)
        self.client = TestClient(create_app(self.service, api_token=TOKEN))
        self.auth = {"Authorization": f"Bearer {TOKEN}", "X-Sentinel-Operator": "tester"}

    def tearDown(self):
        self.service.stop()
        self.directory.cleanup()

    def test_health_is_public_but_data_requires_token(self):
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/site").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/site", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/site", headers=self.auth).status_code, 200)

    def test_session_cookie_login(self):
        self.assertEqual(self.client.post("/api/v1/session", json={"token": "nope"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/session", json={"token": TOKEN}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/settings").status_code, 200)

    def test_site_never_exposes_camera_sources(self):
        body = self.client.get("/api/v1/site", headers=self.auth).json()
        for camera in body["cameras"]:
            self.assertEqual(camera["source_type"], "simulated")
            self.assertFalse({"url", "sub_url", "path", "sub_path", "password", "credential"} & set(camera["source"]))

    def test_settings_patch_is_validated_and_audited(self):
        response = self.client.patch("/api/v1/settings", headers=self.auth, json={"arm_mode": "away"})
        self.assertEqual(response.json()["arm_mode"], "away")
        self.assertEqual(self.client.patch("/api/v1/settings", headers=self.auth, json={"arm_mode": "party"}).status_code, 422)
        audit = self.client.get("/api/v1/audit", headers=self.auth).json()
        self.assertEqual((audit[0]["actor"], audit[0]["action"]), ("tester", "settings_changed"))

    def test_incident_review_flow(self):
        from sentinel.models import SecurityEvent

        runtime = self.service.runtime
        event = SecurityEvent("evt_1", "zone_entry", "high", "cam_backyard", 1000, 1000, 0.9, "Person entered Shed", zone_id="shed", site_track_id="st_1")
        runtime._handle_events([event], 1000)
        incidents = self.client.get("/api/v1/incidents", headers=self.auth).json()
        self.assertEqual(len(incidents), 1)
        incident_id = incidents[0]["incident_id"]
        detail = self.client.get(f"/api/v1/incidents/{incident_id}", headers=self.auth).json()
        self.assertEqual([e["event_id"] for e in detail["events"]], ["evt_1"])
        acked = self.client.post(f"/api/v1/incidents/{incident_id}/acknowledge", headers=self.auth).json()
        self.assertEqual(acked["acknowledged_by"], "tester")
        self.assertEqual(self.client.post(f"/api/v1/incidents/{incident_id}/resolve", headers=self.auth, json={"outcome": "maybe"}).status_code, 422)
        resolved = self.client.post(f"/api/v1/incidents/{incident_id}/resolve", headers=self.auth, json={"outcome": "false_alarm"}).json()
        self.assertEqual(resolved["resolution"], "false_alarm")
        self.assertEqual(self.client.post("/api/v1/incidents/inc_missing/acknowledge", headers=self.auth).status_code, 404)

    def test_high_incident_queues_but_does_not_approve_drone_inspection(self):
        from sentinel.models import SecurityEvent

        event = SecurityEvent("evt_2", "zone_entry", "high", "cam_backyard", 1000, 1000, 0.9, "Person entered Shed", zone_id="shed", site_track_id="st_2")
        self.service.runtime._handle_events([event], 1000)
        drone = self.client.get("/api/v1/drone", headers=self.auth).json()
        self.assertEqual(drone["requests"][0]["status"], "pending_approval")
        self.assertEqual(drone["requests"][0]["target_label"], "Shed")
        self.assertEqual(drone["telemetry"]["flight_mode"], "docked")

    def test_drone_endpoints(self):
        rejected = self.client.post("/api/v1/drone/inspections", headers=self.auth, json={"lat": 46.0, "lon": -75.0})
        self.assertEqual(rejected.status_code, 422)
        created = self.client.post("/api/v1/drone/inspections", headers=self.auth, json={"zone_id": "backyard"}).json()
        approved = self.client.post(f"/api/v1/drone/inspections/{created['request_id']}/approve", headers=self.auth)
        self.assertEqual(approved.json()["status"], "in_progress")
        again = self.client.post(f"/api/v1/drone/inspections/{created['request_id']}/approve", headers=self.auth)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.client.post("/api/v1/drone/commands", headers=self.auth, json={"action": "barrel_roll"}).status_code, 422)
        status = self.client.post("/api/v1/drone/commands", headers=self.auth, json={"action": "hold"}).json()
        self.assertEqual(status["adapter"], "simulated")

    def test_media_path_traversal_is_blocked(self):
        media_root = self.service.runtime.media_root
        (media_root / "snapshots").mkdir(parents=True, exist_ok=True)
        (media_root / "snapshots" / "ok.jpg").write_bytes(b"jpeg")
        self.assertEqual(self.client.get("/api/v1/media/snapshots/ok.jpg", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/media/..%2Fsentinel.db", headers=self.auth).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/media/../sentinel.db", headers=self.auth).status_code, 404)

    def test_dashboard_is_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sentinel", page.text)


if __name__ == "__main__":
    unittest.main()
