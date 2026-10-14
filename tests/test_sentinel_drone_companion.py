"""Drone V1 interfaces: companion pairing and sessions, the MAVLink relay's
command filter, the MAVLink adapter (fake link), the companion agent, config
validation, drone records, the API, and drone video."""

import base64
import copy
import os
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.companion.agent import CompanionAgent, HubError
from sentinel.companion.relay import FrameParser, MavlinkRelay, hub_frame_allowed, x25_crc
from sentinel.config import ConfigValidationError, load_config, validate_config
from sentinel.drone.base import FlightCommandRejected
from sentinel.drone.mavlink import MavlinkTelemetryAdapter, SitlMavlinkAdapter, apply_mavlink_message, is_loopback_endpoint
from sentinel.drone.pairing import CompanionRegistry, PairingError, mavlink_signing_key, session_mac
from sentinel.models import Incident
from sentinel.store import EventStore

try:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink.dialects.v20 import ardupilotmega as mavlink2
except ImportError:  # optional dependency
    mavlink2 = None

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

EXAMPLE = load_config("config/sentinel.example.json", resolve_secrets=False)


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def set(self, name, value):
        self.values[name] = value

    def get(self, name):
        return self.values.get(name)

    def delete(self, name):
        self.values.pop(name, None)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.secrets = MemorySecrets()
        self.saved = {}
        self.registry = CompanionRegistry(self.secrets, save=lambda row: self.saved.__setitem__(row["companion_id"], row), clock=lambda: self.now)

    def session(self, companion_id, secret, *, nonce="nonce-0001", offset_ms=0):
        ts = int(self.now * 1000) + offset_ms
        return self.registry.open_session(companion_id, ts, nonce, session_mac(base64.b64decode(secret), companion_id, ts, nonce))

    def test_pair_session_report_and_revoke(self):
        code = self.registry.create_code("drone_1", "alice")
        self.assertRegex(code["code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        identity = self.registry.pair(code["code"].lower(), "Pi", {"model": "Raspberry Pi Zero 2 W"})
        self.assertEqual(identity["drone_id"], "drone_1")
        self.assertNotIn("secret", self.saved[identity["companion_id"]], "the secret goes to the credential store only")
        with self.assertRaises(PairingError):
            self.registry.pair(code["code"], "again")  # one-time
        token = self.session(identity["companion_id"], identity["secret"])["token"]
        self.registry.report(token, {"health": {"cpu_temp_c": 51.2}, "junk": object()})
        listed = self.registry.list()[0]
        self.assertEqual(listed["state"], "reporting")
        self.assertNotIn("junk", listed["report"])
        self.now += 11
        self.assertEqual(self.registry.list()[0]["state"], "silent")
        self.registry.revoke(identity["companion_id"], "alice")
        with self.assertRaises(PairingError):
            self.registry.report(token, {})
        self.assertEqual(self.secrets.values, {})

    def test_sessions_refuse_replay_wrong_secret_and_stale_clocks(self):
        identity = self.registry.pair(self.registry.create_code("d", "op")["code"], "Pi")
        self.session(identity["companion_id"], identity["secret"], nonce="nonce-aaaa")
        with self.assertRaises(PairingError):
            self.session(identity["companion_id"], identity["secret"], nonce="nonce-aaaa")
        with self.assertRaises(PairingError):
            self.session(identity["companion_id"], base64.b64encode(b"x" * 32).decode(), nonce="nonce-bbbb")
        with self.assertRaises(PairingError):
            self.session(identity["companion_id"], identity["secret"], nonce="nonce-cccc", offset_ms=-300_000)
        self.now += 1801
        with self.assertRaises(PairingError):
            self.registry.authenticate("not-a-token")

    def test_codes_expire_and_guessing_is_limited(self):
        code = self.registry.create_code("d", "op")["code"]
        self.now += 601
        with self.assertRaises(PairingError):
            self.registry.pair(code, "late")
        for _ in range(10):
            with self.assertRaises(PairingError):
                self.registry.pair("AAAA-AAAA", "guess")
        fresh = self.registry.create_code("d", "op")["code"]
        with self.assertRaises(PairingError) as raised:
            self.registry.pair(fresh, "real")
        self.assertIn("too many", str(raised.exception))

    def test_a_replacement_companion_retires_the_old_one(self):
        first = self.registry.pair(self.registry.create_code("d", "op")["code"], "old")
        second = self.registry.pair(self.registry.create_code("d", "op")["code"], "new")
        self.assertEqual(self.registry.for_drone("d").companion_id, second["companion_id"])
        self.assertTrue(self.registry.companions[first["companion_id"]].revoked_ms)
        self.assertEqual(len(mavlink_signing_key(base64.b64decode(second["secret"]))), 32)


def frame(message, version=2):
    mav = mavlink2.MAVLink(None, srcSystem=245, srcComponent=190)
    if version == 1:
        return message.pack(mavlink2.MAVLink(None, srcSystem=245, srcComponent=190, use_native=False), force_mavlink1=True)
    return message.pack(mav)


@unittest.skipIf(mavlink2 is None, "pymavlink not installed (used only to build test frames)")
class RelayFilterTests(unittest.TestCase):
    def parse(self, raw):
        frames = FrameParser().feed(raw)
        self.assertEqual(len(frames), 1)
        return frames[0]

    def test_read_only_traffic_passes(self):
        for message in (mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3),
                        mavlink2.MAVLink_param_request_list_message(1, 1),
                        mavlink2.MAVLink_command_long_message(1, 1, 511, 0, 33, 250000, 0, 0, 0, 0, 0),
                        mavlink2.MAVLink_mission_request_list_message(1, 1, 1)):
            for version in (1, 2):
                allowed, reason = hub_frame_allowed(self.parse(frame(message, version)))
                self.assertTrue(allowed, (message.get_type(), version, reason))

    def test_anything_that_can_fly_the_aircraft_is_dropped(self):
        for message in (mavlink2.MAVLink_command_long_message(1, 1, 400, 0, 1, 0, 0, 0, 0, 0, 0),  # arm
                        mavlink2.MAVLink_command_long_message(1, 1, 22, 0, 0, 0, 0, 0, 0, 0, 10),  # take off
                        mavlink2.MAVLink_command_long_message(1, 1, 176, 0, 1, 4, 0, 0, 0, 0, 0),  # set mode
                        mavlink2.MAVLink_set_mode_message(1, 1, 4),
                        mavlink2.MAVLink_param_set_message(1, 1, b"FENCE_ENABLE", 0, 9),
                        mavlink2.MAVLink_mission_count_message(1, 1, 3, 0),
                        mavlink2.MAVLink_set_position_target_global_int_message(0, 1, 1, 6, 3576, 450000000, -750000000, 10, 0, 0, 0, 0, 0, 0, 0, 0)):
            allowed, _ = hub_frame_allowed(self.parse(frame(message)))
            self.assertFalse(allowed, message.get_type())

    def test_corrupted_frames_are_dropped(self):
        raw = bytearray(frame(mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3)))
        raw[-3] ^= 0xFF
        allowed, reason = hub_frame_allowed(self.parse(bytes(raw)))
        self.assertFalse(allowed)
        self.assertEqual(reason, "bad checksum")

    def test_parser_resynchronises_after_garbage_and_split_reads(self):
        raw = frame(mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3))
        parser = FrameParser()
        self.assertEqual(parser.feed(b"\x00\x01garbage" + raw[:5]), [])
        self.assertEqual(len(parser.feed(raw[5:] + raw)), 2)
        self.assertEqual(x25_crc(b"123456789"), 0x6F91)  # CRC-16/MCRF4XX check value

    def test_live_udp_relay_forwards_telemetry_and_blocks_commands(self):
        fc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fc.bind(("127.0.0.1", 0))
        hub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        hub.bind(("127.0.0.1", 0))
        fc.settimeout(2)
        hub.settimeout(2)
        relay = MavlinkRelay(f"udp:127.0.0.1:{fc.getsockname()[1]}", f"udp:127.0.0.1:{hub.getsockname()[1]}").start()
        try:
            deadline = time.time() + 3
            while relay._fc_sock is None and time.time() < deadline:
                time.sleep(0.05)
            relay_hub = ("127.0.0.1", relay._hub_sock.getsockname()[1])
            hub.sendto(frame(mavlink2.MAVLink_command_long_message(1, 1, 400, 0, 1, 0, 0, 0, 0, 0, 0)), relay_hub)
            hub.sendto(frame(mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3)), relay_hub)
            data, relay_fc = fc.recvfrom(4096)
            self.assertEqual(FrameParser().feed(data)[0].msgid, 0, "only the heartbeat reached the flight controller")
            fc.sendto(frame(mavlink2.MAVLink_sys_status_message(0, 0, 0, 0, 12600, 0, 90, 0, 0, 0, 0, 0, 0)), relay_fc)
            telemetry, _ = hub.recvfrom(4096)
            self.assertEqual(FrameParser().feed(telemetry)[0].msgid, 1)
            status = relay.status()
            self.assertEqual((status["hub_frames_forwarded"], status["hub_frames_dropped"]), (1, 1))
            relay.set_hub_link(False)
            fc.sendto(telemetry, relay_fc)
            with self.assertRaises(socket.timeout):
                hub.recvfrom(4096)
        finally:
            relay.stop()
            fc.close()
            hub.close()

    def test_sitl_policy_needs_a_local_simulator(self):
        with self.assertRaises(ValueError):
            MavlinkRelay("tcp:192.168.1.50:5760", "udp:127.0.0.1:14551", policy="sitl")
        MavlinkRelay("tcp:127.0.0.1:5762", "udp:127.0.0.1:14551", policy="sitl").stop()


def message(kind, src=(1, 1), **fields):
    return SimpleNamespace(get_type=lambda: kind, get_srcSystem=lambda: src[0], get_srcComponent=lambda: src[1], **fields)


class FakeLink:
    def __init__(self, messages, eof_after=None):
        self.messages = queue.Queue()
        for item in messages:
            self.messages.put(item)
        self.sent = []
        self.eof_after = eof_after
        self.reads = 0
        self.closed = False
        self.mav = SimpleNamespace()
        for name in ("heartbeat_send", "request_data_stream_send", "command_long_send", "param_request_list_send", "param_request_read_send",
                     "mission_request_list_send", "mission_request_int_send", "mission_ack_send"):
            setattr(self.mav, name, lambda *args, n=name: self.sent.append((n, args)))

    def handle_eof(self):
        pass

    def recv_match(self, blocking=True, timeout=0.2):
        self.reads += 1
        if self.eof_after is not None and self.reads > self.eof_after:
            self.handle_eof()
            return None
        try:
            return self.messages.get(timeout=0.05)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


class AdapterTests(unittest.TestCase):
    def test_telemetry_parsing_ignores_other_ground_stations_and_the_companion(self):
        from sentinel.drone.base import DroneTelemetry

        t = DroneTelemetry()
        apply_mavlink_message(t, message("HEARTBEAT", base_mode=128, custom_mode=6, autopilot=3, type=2, system_status=4), 1)
        apply_mavlink_message(t, message("HEARTBEAT", base_mode=0, custom_mode=0, autopilot=8, type=18, system_status=4), 2)
        self.assertEqual((t.flight_mode, t.armed, t.vehicle_state, t.last_heartbeat_ms), ("RTL", True, "active", 1))
        apply_mavlink_message(t, message("EXTENDED_SYS_STATE", landed_state=2), 3)
        apply_mavlink_message(t, message("GLOBAL_POSITION_INT", lat=450000000, lon=-750000000, relative_alt=300, hdg=9000, vx=0, vy=0), 3)
        self.assertTrue(t.in_air, "the landed state wins over a low altitude")
        apply_mavlink_message(t, message("GPS_RAW_INT", fix_type=3, satellites_visible=11, eph=90), 4)
        apply_mavlink_message(t, message("EKF_STATUS_REPORT", flags=1 | 2 | 16 | 32), 4)
        apply_mavlink_message(t, message("SYS_STATUS", battery_remaining=80, voltage_battery=12000, onboard_control_sensors_present=0x10000000,
                                         onboard_control_sensors_health=0), 4)
        apply_mavlink_message(t, message("GIMBAL_DEVICE_ATTITUDE_STATUS", q=[0.7071068, 0, -0.7071068, 0], flags=0), 4)
        apply_mavlink_message(t, message("STATUSTEXT", text="PreArm: Need 3D Fix"), 4)
        self.assertEqual((t.hdop, t.ekf_ok, t.prearm_ok, t.message_count), (0.9, True, False, 1))
        self.assertAlmostEqual(t.gimbal_pitch_deg, -90.0, places=0)

    def test_eof_reconnects_with_backoff_and_parameters_prove_a_simulator(self):
        links = []

        def factory(connection, system, component):
            if not links:
                link = FakeLink([], eof_after=1)
            else:
                link = FakeLink([message("HEARTBEAT", base_mode=0, custom_mode=0, autopilot=3, type=2, system_status=3)] +
                                [message("PARAM_VALUE", param_id=name, param_value=1.0, param_index=i, param_count=2)
                                 for i, name in enumerate(["FS_GCS_ENABLE", "SIM_SPEEDUP"])])
            links.append(link)
            return link

        adapter = SitlMavlinkAdapter("tcp:127.0.0.1:5762", allow_flight_commands=True, link_factory=factory)
        adapter.connect()
        try:
            deadline = time.time() + 5
            while not adapter.params_complete() and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(len(links), 2, "reconnected after EOF")
            self.assertTrue(links[0].closed)
            self.assertEqual(adapter.reconnects, 1)
            self.assertTrue(adapter.verified_simulator())
            self.assertFalse(adapter.is_physical)
            self.assertEqual(adapter.commands_allowed()[0], True)
            sent = [name for name, _ in links[1].sent]
            self.assertIn("heartbeat_send", sent)
            self.assertIn("param_request_list_send", sent)
        finally:
            adapter.disconnect()

    def test_command_gate(self):
        adapter = SitlMavlinkAdapter("udpin:0.0.0.0:14551", allow_flight_commands=True)
        adapter._params, adapter._param_index, adapter._param_count = {"SIM_SPEEDUP": 1.0}, {0: "SIM_SPEEDUP"}, 1
        self.assertIn("loopback", adapter.commands_allowed()[1])
        adapter = SitlMavlinkAdapter("tcp:127.0.0.1:5762", allow_flight_commands=False)
        self.assertIn("not enabled", adapter.commands_allowed()[1])
        adapter = SitlMavlinkAdapter("tcp:127.0.0.1:5762", allow_flight_commands=True)
        adapter._params, adapter._param_index, adapter._param_count = {"FS_GCS_ENABLE": 1.0}, {0: "FS_GCS_ENABLE"}, 1
        self.assertIn("proved", adapter.commands_allowed()[1])
        self.assertTrue(adapter.is_physical, "an unproven vehicle is treated as real")
        with self.assertRaises(FlightCommandRejected):
            adapter.takeoff(10)
        for action in (lambda a: a.takeoff(5), lambda a: a.land(), lambda a: a.return_to_home()):
            with self.assertRaises(FlightCommandRejected):
                action(MavlinkTelemetryAdapter("tcp:127.0.0.1:5762"))
        self.assertTrue(is_loopback_endpoint("udpin:127.0.0.1:14551"))
        self.assertFalse(is_loopback_endpoint("COM5"))
        self.assertFalse(is_loopback_endpoint("tcp:10.0.0.5:5760"))


class AgentTests(unittest.TestCase):
    def test_agent_reopens_its_session_after_a_hub_restart(self):
        now = [1_800_000_000.0]
        registry = CompanionRegistry(MemorySecrets(), clock=lambda: now[0])
        identity = registry.pair(registry.create_code("d", "op")["code"], "Pi")

        def post(url, body, token=""):
            try:
                if url.endswith("/session"):
                    return registry.open_session(body["companion_id"], body["timestamp_ms"], body["nonce"], body["mac"])
                registry.report(token, body)
                return {"ok": True}
            except PairingError as error:
                raise HubError(401, str(error)) from None

        import sentinel.companion.agent as agent_module

        real_time = agent_module.time
        agent_module.time = SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic)
        try:
            agent = CompanionAgent(identity | {"hub_url": "http://hub"}, post=post, health=lambda: {"cpu_temp_c": 48.0}, relay_status=lambda: {"hub_frames_dropped": 2})
            agent.report_once()
            registry._sessions.clear()  # the Hub restarted: sessions are in memory only
            agent.report_once()
        finally:
            agent_module.time = real_time
        self.assertEqual(agent.reports_sent, 2)
        self.assertEqual(registry.list()[0]["report"]["relay"]["hub_frames_dropped"], 2)


class ConfigTests(unittest.TestCase):
    def config(self):
        config = copy.deepcopy(EXAMPLE)
        config.pop("_config_dir", None)
        return config

    def test_flight_commands_only_for_sitl(self):
        config = self.config()
        config["drone"] |= {"adapter": "mavlink", "connection": "udpin:127.0.0.1:14551", "allow_flight_commands": True}
        with self.assertRaises(ConfigValidationError) as raised:
            validate_config(config)
        self.assertIn("only possible for an ArduPilot SITL", str(raised.exception))
        config["drone"]["adapter"] = "sitl"
        validate_config(config)
        config["devices"] = [{"id": "real", "category": "drone", "name": "Real", "profile": "mavlink_drone", "connection": "udp:127.0.0.1:14550",
                              "allow_flight_commands": True}]
        with self.assertRaises(ConfigValidationError):
            validate_config(config)

    def test_drone_safety_weather_and_video_are_validated(self):
        config = self.config()
        config["drone"] |= {"safety": {"inspection_altitude_m": 40}, "weather": {"max_wind_mps": 99},
                            "video": {"source": {"type": "rtsp", "url": "rtsp://admin:secret@10.0.0.2/live"}, "camera": {"hfov_deg": 400}}}
        with self.assertRaises(ConfigValidationError) as raised:
            validate_config(config)
        text = str(raised.exception)
        for fragment in ("must not exceed drone.geofence.max_altitude_m", "weather.max_wind_mps", "must not embed a password", "hfov_deg"):
            self.assertIn(fragment, text)


class StoreTests(unittest.TestCase):
    def test_drone_evidence_follows_incident_protection_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "s.db")
            try:
                store.upsert_incident(Incident("inc_1", 1000, 1000, "high", "Person entered Shed"))
                store.upsert_drone_inspection({"request_id": "insp_1", "drone_id": "d", "incident_id": "inc_1", "created_ms": 1000, "status": "completed",
                                               "evidence_paths": ["drone/insp_1/a.jpg"]})
                store.upsert_drone_inspection({"request_id": "insp_2", "drone_id": "d", "incident_id": "", "created_ms": 1000, "status": "denied"})
                self.assertIn("drone/insp_1/a.jpg", store.protected_media())
                store.purge(events_before_ms=10_000, media_before_ms=10_000, media_root=Path(directory) / "media", protected_before_ms=0)
                self.assertEqual([row["request_id"] for row in store.list_drone_inspections()], ["insp_1"], "the open incident keeps its inspection")
            finally:
                store.close()


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class DroneApiTests(unittest.TestCase):
    def setUp(self):
        from sentinel.api import create_app
        from sentinel.service import SentinelService

        self.directory = tempfile.TemporaryDirectory()
        config = copy.deepcopy(EXAMPLE)
        config["storage"] = {"dir": self.directory.name}
        self.service = SentinelService(config, start_cameras=False)
        self.client = TestClient(create_app(self.service, api_token="token-123"))
        self.auth = {"Authorization": "Bearer token-123", "X-Sentinel-Operator": "tester"}

    def tearDown(self):
        self.service.stop()
        self.directory.cleanup()

    def test_companion_endpoints_use_device_authentication(self):
        self.assertEqual(self.client.post("/api/v1/drone/companions/code", json={}).status_code, 401)
        code = self.client.post("/api/v1/drone/companions/code", headers=self.auth, json={}).json()["code"]
        self.assertEqual(self.client.post("/api/v1/drone/companion/pair", json={"code": "WRONG-CODE"}).status_code, 403)
        identity = self.client.post("/api/v1/drone/companion/pair", json={"code": code, "name": "Pi"}).json()
        ts, nonce = int(time.time() * 1000), "nonce-1234"
        mac = session_mac(base64.b64decode(identity["secret"]), identity["companion_id"], ts, nonce)
        token = self.client.post("/api/v1/drone/companion/session", json={"companion_id": identity["companion_id"], "timestamp_ms": ts,
                                                                           "nonce": nonce, "mac": mac}).json()["token"]
        self.assertEqual(self.client.post("/api/v1/drone/companion/report", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/drone/companion/report", json={"health": {"cpu_temp_c": 50}},
                                          headers={"authorization": f"Companion {token}"}).status_code, 200)
        listed = self.client.get("/api/v1/drone/companions", headers=self.auth).json()
        self.assertNotIn("secret", str(listed))
        status = self.client.get("/api/v1/drone", headers=self.auth).json()
        self.assertEqual(status["companion"]["state"], "reporting")
        self.client.delete(f"/api/v1/drone/companions/{identity['companion_id']}", headers=self.auth)
        self.assertEqual(self.client.post("/api/v1/drone/companion/report", json={}, headers={"authorization": f"Companion {token}"}).status_code, 401)

    def test_readiness_exports_logs_and_maintenance(self):
        readiness = self.client.get("/api/v1/drone/readiness", headers=self.auth).json()
        self.assertEqual(readiness["mode"], "command", "the built-in simulator may be flown by Sentinel")
        created = self.client.post("/api/v1/drone/inspections", headers=self.auth, json={"zone_id": "shed"}).json()
        plan = self.client.get(f"/api/v1/drone/inspections/{created['request_id']}/mission", headers=self.auth)
        self.assertIn("attachment", plan.headers["content-disposition"])
        self.assertEqual(plan.json()["sentinel"]["status"], "pending_approval")
        waypoints = self.client.get(f"/api/v1/drone/inspections/{created['request_id']}/mission?format=waypoints", headers=self.auth)
        self.assertTrue(waypoints.text.startswith("QGC WPL 110"))
        route = self.client.get(f"/api/v1/drone/inspections/{created['request_id']}/route", headers=self.auth).json()
        self.assertTrue(route["preview"])
        self.assertEqual(self.client.post("/api/v1/drone/conditions", headers=self.auth, json={"precipitation": False, "visibility_ok": True}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/drone/maintenance", headers=self.auth, json={"kind": "propellers", "note": "new set"}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/drone/maintenance", headers=self.auth, json={"kind": "commissioning"}).status_code, 422,
                         "the built-in simulator has no parameters to commission")
        self.assertEqual(self.client.get("/api/v1/drone/maintenance", headers=self.auth).json()["records"][0]["kind"], "propellers")
        self.assertEqual(self.client.get("/api/v1/drone/flights", headers=self.auth).json(), [])
        self.assertEqual(self.client.get("/api/v1/drone/link", headers=self.auth).json()["state"], "simulated")

    def test_explanation_reports_drone_observations_separately(self):
        store = self.service.runtime.store
        store.upsert_incident(Incident("inc_1", 1000, 2000, "high", "Person entered Shed"))
        store.upsert_drone_inspection({"request_id": "insp_1", "drone_id": "drone", "incident_id": "inc_1", "created_ms": 1500, "status": "completed",
                                       "target_label": "Shed", "mode": "propose", "requested_by": "sentinel", "decided_by": "alice",
                                       "evidence_paths": ["drone/insp_1/a.jpg"],
                                       "observations": {"text": ["Person: up to 1 visible at the same time near Shed."], "max_visible": {"person": 1},
                                                        "observations": [{"snapshot_path": "drone/insp_1/a.jpg", "first_ms": 1600}]}})
        explanation = self.client.get("/api/v1/incidents/inc_1/explanation", headers=self.auth).json()
        self.assertEqual(explanation["drone_inspections"][0]["observations"]["max_visible"], {"person": 1})
        self.assertIn({"type": "drone_snapshot", "path": "drone/insp_1/a.jpg", "camera": "Drone camera", "time_ms": 1600, "request_id": "insp_1"},
                      explanation["evidence"])
        self.assertEqual(explanation["cameras"], [], "the drone is not listed as a fixed camera")


class DroneVideoTests(unittest.TestCase):
    def test_simulated_aerial_inspection_attaches_fingerprinted_evidence(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV not installed")
        from sentinel.drone import DroneManager, Geofence
        from sentinel.drone.base import map_point_to_geo
        from sentinel.drone.manager import DroneSafetyConfig
        from sentinel.drone.mock import SimulatedDrone
        from sentinel.drone.video import DroneVideoWorker

        ORIGIN = (45.0, -75.0)
        FENCE = Geofence([list(map_point_to_geo(ORIGIN, x, y)) for x, y in [[0, 0], [40, 0], [40, 28], [0, 28]]], 25, 5)

        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "s.db")
            drone = SimulatedDrone(map_point_to_geo(ORIGIN, 6, 4), cruise_speed_mps=10)
            drone.connect()
            manager = DroneManager(drone, FENCE, ORIGIN, DroneSafetyConfig(inspection_altitude_m=12, hover_s=2), records=store)
            settings = {"source": {"type": "simulated_aerial", "objects": [{"label": "person", "path": [{"t": 0, "map": [35, 3.5]}]}]}, "fps": 10}
            worker = DroneVideoWorker(manager, settings, config={}, media_root=Path(directory) / "media", record_evidence=store.record_evidence)
            manager.video_status = worker.status
            worker.start()
            try:
                request = manager.request_inspection(requested_by="op", map_point=(35.5, 3), label="Shed", incident_id="inc_1")
                manager.approve(request.request_id, "op")
                deadline = time.time() + 30
                while request.status not in ("completed", "aborted") and time.time() < deadline:
                    manager.step(0.25)
                    time.sleep(0.02)
                self.assertEqual(request.status, "completed")
                deadline = time.time() + 5
                while not request.observations.get("final") and time.time() < deadline:
                    time.sleep(0.05)
            finally:
                worker.stop()
                worker.join(timeout=5)
            summary = request.observations
            self.assertTrue(summary.get("final"))
            self.assertEqual(summary["max_visible"], {"person": 1})
            row = summary["observations"][0]
            self.assertLess(row["position"]["distance_from_target_m"], 3.0)
            self.assertRegex(row["snapshot_path"], r"^drone/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.jpg$", "portable file name (no ':' alternate streams on Windows)")
            self.assertTrue((Path(directory) / "media" / row["snapshot_path"]).is_file())
            self.assertIn(Path(row["snapshot_path"]).name, [p.name for p in (Path(directory) / "media" / "drone").rglob("*.jpg")])
            self.assertIn(row["snapshot_path"], store.evidence_hashes([row["snapshot_path"]]))
            self.assertEqual(worker.state, "idle", "the drone camera is not analysed outside an inspection")
            store.close()


if __name__ == "__main__":
    unittest.main()
