"""Camera onboarding, credential handling, configuration editing, and how
edits reach the running pipeline. Fake RTSP/ONVIF servers stand in for devices."""

import copy
import hashlib
import json
import logging
import re
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sentinel.config import load_config
from sentinel.credentials import SecretStore
from sentinel.models import Detection, SecurityEvent, effective_categories
from sentinel.onboarding import probe_rtsp
from sentinel.onvif import choose_streams, parse_probe_matches
from sentinel.site_config import SiteConfigError, SiteConfigManager
from sentinel.streams import build_url, public_source, scrub, selected_stream

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None

CONFIG = load_config("config/sentinel.example.json", resolve_secrets=False)
SECRET = "S3cr#t:pa/ss"


# ---- fake devices ------------------------------------------------------------------


class FakeRtspServer(threading.Thread):
    """Answers DESCRIBE with Digest auth like a typical IP camera."""

    def __init__(self, username="admin", password=SECRET, path="/Streaming/Channels/102"):
        super().__init__(daemon=True)
        self.username, self.password, self.path = username, password, path
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.start()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        with conn:
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                text = data.decode()
                cseq = re.search(r"CSeq: (\d+)", text).group(1)
                uri = text.split()[1]
                auth = re.search(r"Authorization: Digest (.*)\r\n", text)
                if not auth:
                    conn.sendall(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Digest realm="cam", nonce="abc123"\r\n\r\n'.encode())
                    continue
                fields = dict(re.findall(r'(\w+)="([^"]*)"', auth.group(1)))
                ha1 = hashlib.md5(f"{self.username}:cam:{self.password}".encode()).hexdigest()
                ha2 = hashlib.md5(f"DESCRIBE:{fields['uri']}".encode()).hexdigest()
                if fields.get("username") != self.username or fields.get("response") != hashlib.md5(f"{ha1}:abc123:{ha2}".encode()).hexdigest():
                    conn.sendall(f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n\r\n".encode())
                    continue
                if not uri.endswith(self.path):
                    conn.sendall(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
                    continue
                sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}".encode())

    def close(self):
        self.sock.close()


ONVIF_RESPONSES = {
    "GetSystemDateAndTime": "<tt:UTCDateTime><tt:Time><tt:Hour>12</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>0</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>13</tt:Day></tt:Date></tt:UTCDateTime>",
    "GetDeviceInformation": "<tds:Manufacturer>Acme</tds:Manufacturer><tds:Model>Cam-1</tds:Model><tds:FirmwareVersion>1.2</tds:FirmwareVersion>",
    "GetCapabilities": "<tt:Media><tt:XAddr>http://127.0.0.1:{port}/onvif/media</tt:XAddr></tt:Media>",
    "GetProfiles": (
        '<trt:Profiles token="main"><tt:Name>Main</tt:Name><tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>'
        "<tt:Resolution><tt:Width>2560</tt:Width><tt:Height>1440</tt:Height></tt:Resolution><tt:RateControl><tt:FrameRateLimit>20</tt:FrameRateLimit></tt:RateControl>"
        '</tt:VideoEncoderConfiguration></trt:Profiles><trt:Profiles token="sub"><tt:Name>Sub</tt:Name><tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>'
        "<tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>"
    ),
    "GetStreamUri": "<trt:MediaUri><tt:Uri>rtsp://127.0.0.1:554/live/{token}</tt:Uri></trt:MediaUri>",
}


class FakeOnvif(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        action = next(name for name in ONVIF_RESPONSES if f"<{name}" in body)
        if action != "GetSystemDateAndTime" and "PasswordDigest" not in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<s:Envelope><s:Body><s:Fault>ter:NotAuthorized</s:Fault></s:Body></s:Envelope>")
            return
        token = re.search(r"<ProfileToken>(\w+)</ProfileToken>", body)
        payload = ONVIF_RESPONSES[action].format(port=self.server.server_port, token=token.group(1) if token else "")
        xml = (
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
            f"<s:Body><r>{payload}</r></s:Body></s:Envelope>"
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(xml.encode())

    def log_message(self, *args):
        pass


def make_video(path: Path, frames: int = 20) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 240))
    for index in range(frames):
        image = np.full((240, 320, 3), 60, np.uint8)
        cv2.rectangle(image, (10 + index * 5, 80), (50 + index * 5, 200), (200, 200, 200), -1)
        writer.write(image)
    writer.release()


# ---- streams and credentials ----------------------------------------------------------


class StreamTests(unittest.TestCase):
    def test_template_urls_substream_and_secret_injection(self):
        source = {"type": "rtsp", "host": "10.0.0.5", "template": "hikvision", "channel": 3, "username": "viewer", "credential": "camera.x"}
        secrets = {"camera.x": SECRET}.get
        self.assertEqual(selected_stream(source), "sub")
        self.assertEqual(build_url(source, "sub", secrets), "rtsp://viewer:S3cr%23t%3Apa%2Fss@10.0.0.5:554/Streaming/Channels/302")
        self.assertEqual(build_url(dict(source, prefer_substream=False), selected_stream(dict(source, prefer_substream=False)), secrets).rsplit("/", 1)[1], "301")
        dahua = {"type": "rtsp", "host": "h", "template": "dahua", "channel": 2}
        self.assertTrue(build_url(dahua, "sub").endswith("/cam/realmonitor?channel=2&subtype=1"))
        legacy = {"type": "rtsp", "url": "rtsp://admin@h:554/x", "credential": "camera.x"}
        self.assertEqual(build_url(legacy, "main", secrets), "rtsp://admin:S3cr%23t%3Apa%2Fss@h:554/x")
        with self.assertRaises(KeyError):
            build_url(source, "main", lambda name: None)

    def test_public_source_never_contains_addresses_or_secrets(self):
        source = {"type": "rtsp", "host": "10.0.0.5", "path": "/secret/path", "sub_path": "/s", "url": "rtsp://u@h/x", "credential": "camera.x", "username": "u"}
        view = public_source(source)
        self.assertFalse({"path", "sub_path", "url", "credential", "password"} & set(view))
        self.assertTrue(view["has_credentials"])
        self.assertNotIn("/secret/path", json.dumps(view))
        self.assertEqual(scrub("cannot open rtsp://admin:pw@1.2.3.4/x now"), "cannot open rtsp://[redacted] now")

    def test_credential_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SecretStore(Path(directory) / "credentials.json")
            store.set("camera.front", SECRET)
            self.assertEqual(store.get("camera.front"), SECRET)
            if sys.platform == "win32":
                self.assertNotIn("S3cr", (Path(directory) / "credentials.json").read_text())
            store.delete("camera.front")
            self.assertIsNone(store.get("camera.front"))
            with self.assertRaises(ValueError):
                store.set("../bad", "x")


# ---- RTSP diagnostics against a fake camera -----------------------------------------


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeRtspServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def url(self, user="admin", password=SECRET, path="/Streaming/Channels/102"):
        from urllib.parse import quote

        credentials = f"{user}:{quote(password, safe='')}@" if user else ""
        return f"rtsp://{credentials}127.0.0.1:{self.server.port}{path}"

    def test_classifies_camera_answers(self):
        ok = probe_rtsp(self.url())
        self.assertEqual((ok["kind"], ok["codec"]), ("ok", "H264"))
        self.assertEqual(probe_rtsp(self.url(password="wrong"))["kind"], "auth_failed")
        self.assertEqual(probe_rtsp(self.url(user=""))["kind"], "auth_required")
        self.assertEqual(probe_rtsp(self.url(path="/nope"))["kind"], "not_found")
        for result in (ok, probe_rtsp(self.url(password="wrong"))):
            self.assertNotIn(SECRET, json.dumps(result))

    def test_closed_port_and_non_rtsp_service(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        self.assertIn(probe_rtsp(f"rtsp://127.0.0.1:{port}/x", timeout=2)["kind"], {"port_closed", "unreachable"})
        http = HTTPServer(("127.0.0.1", 0), FakeOnvif)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            self.assertEqual(probe_rtsp(f"rtsp://127.0.0.1:{http.server_port}/x", timeout=3)["kind"], "not_rtsp")
        finally:
            http.shutdown()


# ---- ONVIF -------------------------------------------------------------------------------


class OnvifTests(unittest.TestCase):
    def test_probe_match_parsing(self):
        payload = b"""<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
        <e:Body><d:ProbeMatches><d:ProbeMatch><d:XAddrs>http://192.168.1.64/onvif/device_service http://[fe80::1]/onvif</d:XAddrs>
        <d:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/DS-2CD</d:Scopes></d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"""
        device = parse_probe_matches(payload)[0]
        self.assertEqual((device["host"], device["name"], device["hardware"]), ("192.168.1.64", "Front Door", "DS-2CD"))

    def test_inspect_reads_profiles_and_picks_main_and_sub(self):
        from sentinel import onvif

        server = HTTPServer(("127.0.0.1", 0), FakeOnvif)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            info = onvif.inspect(f"http://127.0.0.1:{server.server_port}/onvif/device_service", "admin", SECRET)
            self.assertEqual(info["device"]["Manufacturer"], "Acme")
            self.assertEqual(info["streams"]["main"]["path"], "/live/main")
            self.assertEqual(info["streams"]["sub"]["profile"]["width"], 640)
            with self.assertRaises(onvif.OnvifError):
                onvif.inspect(f"http://127.0.0.1:{server.server_port}/onvif/device_service", "", "")
        finally:
            server.shutdown()

    def test_choose_streams(self):
        main, sub = choose_streams([{"width": 640, "height": 360}, {"width": 1920, "height": 1080}, {"width": 176, "height": 144}])
        self.assertEqual((main["width"], sub["width"]), (1920, 640))


# ---- site configuration editing ------------------------------------------------------------


class SiteConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.secrets = SecretStore(Path(self.directory.name) / "credentials.json")
        self.manager = SiteConfigManager(copy.deepcopy(CONFIG), self.directory.name, self.secrets)
        self.applied = []
        self.manager.subscribe(self.applied.append)

    def tearDown(self):
        self.directory.cleanup()

    def test_pasted_url_password_moves_to_credential_store(self):
        draft, source, password = self.manager.make_draft({"type": "rtsp", "url": f"rtsp://admin:{SECRET.replace('#', '%23').replace('/', '%2F')}@10.0.0.9:554/live"})
        self.assertEqual(password, SECRET)
        self.assertNotIn("S3cr", json.dumps(source))
        result = self.manager.save_camera(None, {"name": "Garage", "draft_id": draft}, "tester")
        saved = (Path(self.directory.name) / "site.json").read_text()
        self.assertNotIn("S3cr", saved)
        camera = next(c for c in self.manager.config["cameras"] if c["id"] == result["id"])
        self.assertEqual(self.secrets.get(camera["source"]["credential"]), SECRET)
        self.assertIn(":S3cr", build_url(camera["source"], "main", self.secrets.get).replace("%23", "#"))
        self.assertNotIn("S3cr", json.dumps(self.manager.public_view()))
        self.assertEqual(len(self.applied), 1)

    def test_editing_without_password_keeps_credential_and_paths(self):
        draft, _, _ = self.manager.make_draft({"type": "rtsp", "host": "10.0.0.9", "template": "custom", "path": "/a", "sub_path": "/b", "username": "u"}, SECRET)
        camera_id = self.manager.save_camera(None, {"name": "Side", "draft_id": draft}, "t")["id"]
        self.manager.save_camera(camera_id, {"source": {"type": "rtsp", "host": "10.0.0.10", "template": "custom", "username": "u"}}, "t")
        source = self.manager.config_source(camera_id)
        self.assertEqual((source["host"], source["path"], source["sub_path"]), ("10.0.0.10", "/a", "/b"))
        self.assertEqual(self.secrets.get(source["credential"]), SECRET)

    def test_removing_a_zone_disables_rules_that_only_watched_it(self):
        camera = next(c for c in self.manager.config["cameras"] if c["id"] == "cam_backyard")
        zones = [z for z in camera["zones"] if z["id"] != "side_gate"]
        result = self.manager.set_zones("cam_backyard", zones, [], "t")
        rules = {rule["id"]: rule for rule in self.manager.config["rules"]}
        self.assertFalse(rules["perimeter_walk"]["enabled"], "a sequence through a deleted zone cannot run")
        self.assertEqual(rules["backyard_after_hours"]["zones"], ["backyard"])
        self.assertTrue(result["notes"])

    def test_new_zones_get_ids_and_bad_zones_are_rejected_atomically(self):
        camera = next(c for c in self.manager.config["cameras"] if c["id"] == "cam_front")
        zones = camera["zones"] + [{"name": "Mail box", "type": "restricted", "polygon": [[0, 0.8], [0.1, 0.8], [0.1, 1]]}]
        ids = self.manager.set_zones("cam_front", zones, [[[0.9, 0], [1, 0], [1, 0.1]]], "t")["zones"]
        self.assertIn("cam_front_mail_box", ids)
        before = json.dumps(self.manager.config, sort_keys=True)
        with self.assertRaises(SiteConfigError):
            self.manager.set_zones("cam_front", [{"name": "x", "type": "moat", "polygon": [[0, 0], [1, 0], [1, 1]]}], [], "t")
        self.assertEqual(json.dumps(self.manager.config, sort_keys=True), before)

    def test_delete_camera_cleans_links_and_credential(self):
        draft, _, _ = self.manager.make_draft({"type": "rtsp", "host": "10.0.0.9", "template": "hikvision", "username": "u"}, SECRET)
        camera_id = self.manager.save_camera(None, {"name": "Extra", "draft_id": draft}, "t")["id"]
        self.manager.set_topology([{"from": camera_id, "to": "cam_front", "max_transit_s": 20}], "t")
        self.manager.delete_camera(camera_id, "t")
        self.assertEqual(self.manager.config["topology"]["links"], [])
        self.assertEqual(self.secrets.names(), [])

    def test_map_image_and_validation(self):
        with self.assertRaises(SiteConfigError):
            self.manager.set_map_image(b"<svg>not allowed</svg>", "t")
        self.manager.set_map_image(b"\x89PNG\r\n\x1a\n" + b"0" * 32, "t")
        self.assertTrue(self.manager.map_image_path().name.endswith(".png"))
        self.manager.set_map({"width_m": 50, "cameras": [{"id": "cam_front", "x": 10, "y": 5, "heading_deg": 90, "fov_deg": 80}]}, "t")
        camera = next(c for c in self.manager.config["cameras"] if c["id"] == "cam_front")
        self.assertEqual(camera["map"]["heading_deg"], 90)
        with self.assertRaises(SiteConfigError):
            self.manager.set_map({"width_m": 0}, "t")

    def test_managed_copy_wins_on_restart(self):
        self.manager.reorder(["cam_backyard", "cam_front", "cam_driveway"], "t")
        effective = SiteConfigManager.load_effective(copy.deepcopy(CONFIG), self.directory.name)
        self.assertEqual(effective["cameras"][0]["id"], "cam_backyard")

    def test_categories_validated(self):
        with self.assertRaises(SiteConfigError):
            self.manager.set_categories({"dragons": {"detect": True}}, "t")
        self.manager.set_categories({"animal": {"detect": True, "alerts": True}}, "t", "cam_backyard")
        merged = effective_categories(self.manager.config, next(c for c in self.manager.config["cameras"] if c["id"] == "cam_backyard"))
        self.assertTrue(merged["animal"]["alerts"])
        self.assertFalse(effective_categories(self.manager.config)["animal"]["alerts"])


# ---- categories in the pipeline -----------------------------------------------------


class CategoryTests(unittest.TestCase):
    def test_filters_and_detector_allowlist(self):
        from sentinel.engine import ZoneMap
        from sentinel.pipeline import filter_detections, with_category_classes

        categories = effective_categories({"categories": {"animal": {"detect": False, "alerts": False}}})
        enabled = {name for name, value in categories.items() if value["detect"]}
        detections = [Detection("person", 0.9, (0.1, 0.1, 0.1, 0.3)), Detection("dog", 0.9, (0.5, 0.5, 0.1, 0.1))]
        kept = filter_detections(detections, "cam", ZoneMap({"cameras": []}), 0.0, enabled)
        self.assertEqual([d.label for d in kept], ["person"])
        classes = with_category_classes({"classes": ["person", "dog", "car"]}, categories)["classes"]
        self.assertEqual(classes, ["person", "car"])

    def test_non_alerting_categories_are_downgraded_to_info(self):
        from sentinel.runtime import SentinelRuntime

        with tempfile.TemporaryDirectory() as directory:
            runtime = SentinelRuntime(copy.deepcopy(CONFIG), storage_dir=directory, notifiers=[])
            try:
                dog = SecurityEvent("e1", "zone_entry", "high", "cam_backyard", 1000, 1000, 0.9, "Animal (dog) entered Shed", label="animal", site_track_id="st_9")
                person = SecurityEvent("e2", "zone_entry", "high", "cam_backyard", 1000, 1000, 0.9, "Person entered Shed", label="person", site_track_id="st_8")
                runtime._handle_events([dog, person], 1000)
                self.assertEqual((dog.severity, dog.details["original_severity"]), ("info", "high"))
                self.assertIn("category_alerts_off", dog.reason_codes)
                self.assertEqual(person.severity, "high")
                incidents = runtime.store.list_incidents()
                self.assertEqual([i["site_track_ids"] for i in incidents], [["st_8"]], "the dog opened no incident")
            finally:
                runtime.close()

    def test_scene_conditions_are_info_only(self):
        from sentinel.runtime import SentinelRuntime

        with tempfile.TemporaryDirectory() as directory:
            runtime = SentinelRuntime(copy.deepcopy(CONFIG), storage_dir=directory, notifiers=[])
            try:
                runtime.scene_update("cam_front", {"lighting_change": True, "precipitation_or_noise": False}, 1000)
                runtime.scene_update("cam_front", {"lighting_change": False}, 2000)
                runtime.scene_update("cam_front", {"lighting_change": True}, 3000)  # within 10 min: not logged again
                events = runtime.store.list_events()
                self.assertEqual([(e["event_type"], e["severity"]) for e in events], [("scene_condition", "info")])
                self.assertEqual(runtime.store.list_incidents(), [])
                self.assertEqual(runtime.camera_status["cam_front"]["scene"], ["lighting_change"])
            finally:
                runtime.close()


# ---- API and live pipeline integration -------------------------------------------------


@unittest.skipIf(cv2 is None, "OpenCV not installed")
class SetupApiTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        config = copy.deepcopy(CONFIG)
        config["storage"] = {"dir": self.directory.name}
        config["drone"]["enabled"] = False
        self.service = SentinelService(config, start_cameras=True)
        self.service.start()
        self.client = TestClient(create_app(self.service, api_token="t"))
        self.client.headers.update({"Authorization": "Bearer t", "X-Sentinel-Operator": "installer"})
        self.rtsp = FakeRtspServer()

    def tearDown(self):
        self.service.stop()
        self.rtsp.close()
        self.directory.cleanup()

    def test_wizard_flow_adds_a_running_camera_that_uses_new_zones(self):
        video = Path(self.directory.name) / "clip.avi"
        make_video(video)
        test = self.client.post("/api/v1/onboarding/test", json={"source": {"type": "file", "path": str(video)}}).json()
        self.assertTrue(test["ok"], test)
        self.assertEqual(test["streams"]["main"]["resolution"], [320, 240])
        self.assertTrue(test["snapshot_jpeg_b64"])
        camera_id = self.client.post("/api/v1/config/cameras", json={"name": "Shop floor", "fps": 5, "draft_id": test["draft_id"]}).json()["id"]
        self.assertIn(camera_id, self.service.workers)
        zones = [{"name": "Till", "type": "restricted", "polygon": [[0.6, 0.2], [0.95, 0.2], [0.95, 0.95], [0.6, 0.95]]}]
        saved = self.client.put(f"/api/v1/config/cameras/{camera_id}/zones", json={"zones": zones, "privacy_masks": []}).json()
        self.assertIn(saved["zones"][0], self.service.runtime.engine.zones.zones, "zone edit reached the running engine")
        self.assertIn(camera_id, self.service.runtime.camera_configs)
        audit = [row["action"] for row in self.client.get("/api/v1/audit").json()]
        self.assertIn("config_changed", audit)
        deadline = __import__("time").time() + 10
        while __import__("time").time() < deadline and self.client.get(f"/api/v1/cameras/{camera_id}/frame.jpg").status_code != 200:
            __import__("time").sleep(0.2)
        self.assertEqual(self.client.get(f"/api/v1/cameras/{camera_id}/frame.jpg").headers["content-type"], "image/jpeg")

    def test_simulated_camera_through_the_wizard(self):
        test = self.client.post("/api/v1/onboarding/test", json={"source": {"type": "simulated", "scenario": "scenarios/demo_loop.json"}}).json()
        self.assertTrue(test["ok"])
        camera_id = self.client.post("/api/v1/config/cameras", json={"name": "Sim yard", "draft_id": test["draft_id"]}).json()["id"]
        status = self.client.get("/api/v1/cameras").json()
        self.assertIn(camera_id, [camera["id"] for camera in status])

    def test_rtsp_test_reports_diagnostics_without_leaking_secrets(self):
        logs = []
        handler = logging.Handler()
        handler.emit = lambda record: logs.append(record.getMessage())
        logging.getLogger("sentinel").addHandler(handler)
        try:
            source = {"type": "rtsp", "host": "127.0.0.1", "port": self.rtsp.port, "template": "hikvision", "username": "admin", "password": "wrong-password"}
            bad = self.client.post("/api/v1/onboarding/test", json={"source": source})
            self.assertEqual(bad.json()["streams"]["sub"]["kind"], "auth_failed")
            source["password"] = SECRET
            good = self.client.post("/api/v1/onboarding/test", json={"source": source})
            self.assertEqual(good.json()["streams"]["sub"]["kind"], "open_failed", "probe passes; the fake server has no media")
            camera_id = self.client.post("/api/v1/config/cameras", json={"name": "Porch", "draft_id": good.json()["draft_id"]}).json()["id"]
            __import__("time").sleep(1.5)  # let the worker try and fail to connect
            everything = json.dumps([bad.json(), good.json(), self.client.get("/api/v1/config").json(), self.client.get("/api/v1/cameras").json(),
                                     self.client.get("/api/v1/audit").json()]) + "\n".join(logs)
            for secret in (SECRET, "wrong-password", "Streaming/Channels"):
                self.assertNotIn(secret, everything)
            self.assertNotIn(SECRET, (Path(self.directory.name) / "site.json").read_text())
            self.assertEqual(self.service.runtime.secrets.get(f"camera.{camera_id}"), SECRET)
        finally:
            logging.getLogger("sentinel").removeHandler(handler)

    def test_config_errors_are_reported_not_applied(self):
        response = self.client.put("/api/v1/config/topology", json={"links": [{"from": "cam_front", "to": "cam_front", "max_transit_s": 10}]})
        self.assertEqual(response.status_code, 422)
        response = self.client.put("/api/v1/config/cameras/nope/zones", json={"zones": []})
        self.assertEqual(response.status_code, 404)
        response = self.client.put("/api/v1/config/map/image", content=b"GIF89a....", headers={"Content-Type": "image/gif"})
        self.assertEqual(response.status_code, 422)

    def test_category_change_reaches_runtime(self):
        self.client.put("/api/v1/config/categories", json={"categories": {"animal": {"detect": True, "alerts": True}}})
        self.assertTrue(effective_categories(self.service.runtime.config)["animal"]["alerts"])
        unchanged = self.service.workers["cam_front"]
        self.client.put("/api/v1/config/cameras/cam_driveway/zones", json={"zones": [], "privacy_masks": []})
        self.assertIs(self.service.workers["cam_front"], unchanged, "editing one camera does not restart the others")


if __name__ == "__main__":
    unittest.main()
