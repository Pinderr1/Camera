"""Drone reliability: telemetry validation in the MAVLink adapter (invalid and
jumping positions, implausible battery readings, flight-controller restarts,
other autopilots on the link, signed links, the read-only command-path probe)
and the flight-health monitor (staleness, navigation, battery drain).
No simulator needed; tests/test_sentinel_drone_faults_sitl.py injects the same
faults into ArduPilot SITL."""

import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.drone.base import DroneTelemetry, map_point_to_geo, valid_coordinates
from sentinel.drone.flight_health import FlightHealthMonitor, HealthLimits
from sentinel.drone.mavlink import PROBE_COMMAND, MavlinkTelemetryAdapter, apply_mavlink_message

ORIGIN = (45.0, -75.0)
HOME = map_point_to_geo(ORIGIN, 6, 4)


def message(kind, src=(1, 1), **fields):
    return SimpleNamespace(get_type=lambda: kind, get_srcSystem=lambda: src[0], get_srcComponent=lambda: src[1], **fields)


def heartbeat(src=(1, 1), vehicle_type=2, **fields):
    values = {"base_mode": 0, "custom_mode": 0, "autopilot": 3, "type": vehicle_type, "system_status": 3} | fields
    return message("HEARTBEAT", src, **values)


def position(lat, lon, alt_mm=0, boot_ms=None):
    extra = {} if boot_ms is None else {"time_boot_ms": boot_ms}
    return message("GLOBAL_POSITION_INT", lat=int(lat * 1e7), lon=int(lon * 1e7), relative_alt=alt_mm, hdg=0, vx=0, vy=0, **extra)


class TelemetryValidationTests(unittest.TestCase):
    def test_zero_and_out_of_range_positions_are_ignored_and_counted(self):
        t = DroneTelemetry()
        apply_mavlink_message(t, position(0, 0, 1500), 1000)
        self.assertIsNone(t.lat, "ArduPilot sends 0,0 before it has a position")
        self.assertEqual((t.position_rejected, t.telemetry_ms, t.alt_m), (1, 0, 1.5), "barometric height is still used")
        apply_mavlink_message(t, position(*HOME), 2000)
        self.assertEqual((round(t.lat, 6), t.telemetry_ms), (round(HOME[0], 6), 2000))
        apply_mavlink_message(t, message("GLOBAL_POSITION_INT", lat=950000000, lon=0, relative_alt=0, hdg=0, vx=0, vy=0), 3000)
        self.assertEqual((t.position_rejected, t.telemetry_ms), (2, 2000), "the last good position is kept and ages")
        self.assertFalse(valid_coordinates(float("nan"), 1.0))

    def test_position_jump_is_flagged_but_followed(self):
        t = DroneTelemetry()
        apply_mavlink_message(t, position(*HOME), 1000)
        apply_mavlink_message(t, position(*map_point_to_geo(ORIGIN, 7, 4)), 1250)
        self.assertEqual(t.position_jumps, 0)
        far = map_point_to_geo(ORIGIN, 400, 4)  # ~394 m in a quarter second
        apply_mavlink_message(t, position(*far), 1500)
        self.assertEqual((t.position_jumps, t.last_jump_ms), (1, 1500))
        self.assertAlmostEqual(t.lat, far[0], places=6, msg="the jump is reported, not hidden")
        apply_mavlink_message(t, position(*map_point_to_geo(ORIGIN, 420, 4)), 61500)
        self.assertEqual(t.position_jumps, 1, "after a long gap a large move is plausible")

    def test_implausible_battery_readings_are_rejected(self):
        t = DroneTelemetry()
        sys_status = dict(onboard_control_sensors_present=0, onboard_control_sensors_health=0)
        apply_mavlink_message(t, message("SYS_STATUS", battery_remaining=-1, voltage_battery=65535, current_battery=-1, **sys_status), 1000)
        self.assertEqual((t.battery_pct, t.battery_ms, t.battery_rejected), (None, 0, 0), "not reported is not rejected")
        apply_mavlink_message(t, message("SYS_STATUS", battery_remaining=180, voltage_battery=900000 // 10, current_battery=1250, **sys_status), 2000)
        self.assertEqual((t.battery_pct, t.battery_rejected), (None, 2), "180% and 90 V are faults")
        self.assertEqual(t.battery_current_a, 12.5)
        apply_mavlink_message(t, message("SYS_STATUS", battery_remaining=76, voltage_battery=15800, current_battery=900, **sys_status), 3000)
        self.assertEqual((t.battery_pct, t.battery_voltage, t.battery_ms), (76.0, 15.8, 3000))

    def test_boot_clock_going_back_is_a_flight_controller_restart(self):
        t = DroneTelemetry(home_lat=HOME[0], home_lon=HOME[1])
        apply_mavlink_message(t, message("ATTITUDE", roll=0, pitch=0, time_boot_ms=600_000), 1000)
        apply_mavlink_message(t, message("ATTITUDE", roll=0, pitch=0, time_boot_ms=599_000), 1100)  # reordered packet, not a restart
        self.assertEqual(t.fc_reboots, 0)
        apply_mavlink_message(t, message("ATTITUDE", roll=0, pitch=0, time_boot_ms=4_000), 2000)
        self.assertEqual(t.fc_reboots, 1)
        self.assertIsNone(t.home_lat, "home is reset by a restart")
        self.assertIn("restarted", t.messages[-1])

    def test_gimbal_garbage_and_signed_frames(self):
        t = DroneTelemetry()
        apply_mavlink_message(t, message("GIMBAL_DEVICE_ATTITUDE_STATUS", q=[float("nan"), 0, 0, 0], flags=0), 1000)
        self.assertEqual((t.gimbal_pitch_deg, t.gimbal_ms), (None, 0))
        apply_mavlink_message(t, message("GIMBAL_DEVICE_ATTITUDE_STATUS", q=[0.7071068, 0, -0.7071068, 0], flags=0), 1000)
        self.assertEqual(t.gimbal_ms, 1000)
        signed = heartbeat(_header=SimpleNamespace(incompat_flags=1), _msgbuf=bytearray(b"\xfd\x09\x01"))
        apply_mavlink_message(t, signed, 1000)
        self.assertTrue(t.link_signed)
        apply_mavlink_message(t, heartbeat(_header=SimpleNamespace(incompat_flags=0), _msgbuf=bytearray(b"\xfd")), 1100)
        self.assertFalse(t.link_signed)


class FakeLink:
    """A scripted link; `feed` adds messages while the adapter runs."""

    def __init__(self, messages=(), on_send=None):
        self.queue = queue.Queue()
        for item in messages:
            self.queue.put(item)
        self.sent = []
        self.on_send = on_send
        self.mav = SimpleNamespace()
        for name in ("heartbeat_send", "request_data_stream_send", "command_long_send", "param_request_list_send", "param_request_read_send",
                     "mission_request_list_send", "mission_request_int_send", "mission_ack_send"):
            setattr(self.mav, name, lambda *args, n=name: self._sent(n, args))

    def _sent(self, name, args):
        self.sent.append((name, args))
        if self.on_send:
            self.on_send(name, args)

    def feed(self, *messages):
        for item in messages:
            self.queue.put(item)

    def recv_match(self, blocking=True, timeout=0.2):
        try:
            return self.queue.get(timeout=0.02)
        except queue.Empty:
            return None

    def close(self):
        pass


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class AdapterReliabilityTests(unittest.TestCase):
    def adapter(self, link):
        adapter = MavlinkTelemetryAdapter("tcp:127.0.0.1:5762", link_factory=lambda *a: link)
        adapter.connect()
        self.addCleanup(adapter.disconnect)
        return adapter

    def params(self, count=2, names=("FS_GCS_ENABLE", "FENCE_ENABLE")):
        return [message("PARAM_VALUE", param_id=name, param_value=1.0, param_index=i, param_count=count) for i, name in enumerate(names)]

    def test_restart_discards_settings_and_asks_again(self):
        link = FakeLink([heartbeat(), message("ATTITUDE", roll=0, pitch=0, time_boot_ms=900_000)] + self.params())
        adapter = self.adapter(link)
        self.assertTrue(wait_for(adapter.params_complete))
        requests_before = sum(1 for name, _ in link.sent if name == "param_request_list_send")
        link.feed(*(message("ATTITUDE", roll=0, pitch=0, time_boot_ms=3_000 + i * 100) for i in range(3)))
        self.assertTrue(wait_for(lambda: adapter.telemetry().fc_reboots == 1))
        self.assertEqual(adapter.parameters(), {}, "nothing is audited from before the restart")
        self.assertTrue(wait_for(lambda: sum(1 for name, _ in link.sent if name == "param_request_list_send") > requests_before))
        link.feed(*self.params())
        self.assertTrue(wait_for(adapter.params_complete))

    def test_second_autopilot_is_reported_and_never_mixed_in(self):
        link = FakeLink([heartbeat(), position(*HOME)])
        adapter = self.adapter(link)
        self.assertTrue(wait_for(lambda: adapter.telemetry().lat is not None))
        link.feed(heartbeat(src=(7, 1), base_mode=128), message("GLOBAL_POSITION_INT", src=(7, 1), lat=0, lon=0, relative_alt=90000, hdg=0, vx=0, vy=0))
        self.assertTrue(wait_for(lambda: adapter.telemetry().other_vehicles == ["7/1"]))
        telemetry = adapter.telemetry()
        self.assertFalse(telemetry.armed, "the other aircraft's heartbeat did not arm ours")
        self.assertEqual(telemetry.alt_m, 0)

    def test_stream_rates_are_requested_from_copters_only(self):
        copter, plane = FakeLink([heartbeat(vehicle_type=2)]), FakeLink([heartbeat(vehicle_type=1)])
        self.adapter(copter)
        self.adapter(plane)
        self.assertTrue(wait_for(lambda: any(n == "command_long_send" for n, _ in plane.sent)))
        self.assertTrue(wait_for(lambda: any(n == "request_data_stream_send" for n, _ in copter.sent)))
        self.assertFalse(any(n == "request_data_stream_send" for n, _ in plane.sent), "ArduPlane would save the rates as parameters")

    def test_probe_reports_open_blocked_and_ignores_acks_for_the_pilot(self):
        replies = {"answer": True}
        link = FakeLink([])

        def on_send(name, args):
            if name == "command_long_send" and args[2] == PROBE_COMMAND:
                # the pilot's ground station also gets an acknowledgement on the shared link: never ours
                link.feed(message("COMMAND_ACK", command=PROBE_COMMAND, result=3, target_system=255))
                if replies["answer"]:
                    link.feed(message("COMMAND_ACK", command=PROBE_COMMAND, result=3, target_system=245))

        link.on_send = on_send
        adapter = self.adapter(link)
        self.assertEqual(adapter.probe_command_path(0.5)["state"], "unknown", "no link yet")
        heartbeats = threading.Event()

        def beat():
            while not heartbeats.is_set():
                link.feed(heartbeat())
                time.sleep(0.2)

        threading.Thread(target=beat, daemon=True).start()
        self.addCleanup(heartbeats.set)
        self.assertTrue(wait_for(lambda: adapter.telemetry().link_ok))
        opened = adapter.probe_command_path(1.0)
        self.assertEqual((opened["state"], opened["result"]), ("open", 3), "even 'unsupported' proves the path is open")
        replies["answer"] = False
        self.assertEqual(adapter.probe_command_path(0.6)["state"], "blocked")
        sent = [args[2] for name, args in link.sent if name == "command_long_send"]
        self.assertTrue(set(sent) <= {511, 512, PROBE_COMMAND}, f"only read-only requests were sent: {sent}")

    def test_only_one_fence_download_at_a_time(self):
        link = FakeLink([heartbeat()])
        adapter = self.adapter(link)
        self.assertTrue(wait_for(lambda: adapter._vehicle is not None))
        self.assertTrue(adapter._fence_busy.acquire(blocking=False))
        try:
            adapter._download_fence()  # returns at once: one is already running
            self.assertFalse(any(n == "mission_request_list_send" for n, _ in link.sent))
        finally:
            adapter._fence_busy.release()


class RelaySenderTests(unittest.TestCase):
    def test_only_the_configured_pilot_and_hub_reach_the_flight_controller(self):
        import socket

        from sentinel.companion.relay import MavlinkRelay

        def udp():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(1.0)
            self.addCleanup(sock.close)
            return sock

        fc, hub, pilot, stranger = udp(), udp(), udp(), udp()
        relay = MavlinkRelay(f"udp:127.0.0.1:{fc.getsockname()[1]}", f"udp:127.0.0.1:{hub.getsockname()[1]}",
                             [f"udp:127.0.0.1:{pilot.getsockname()[1]}"]).start()
        self.addCleanup(relay.stop)
        self.assertTrue(wait_for(lambda: relay._fc_sock is not None))
        relay_gcs = ("127.0.0.1", relay._gcs_socks[0][0].getsockname()[1])
        relay_hub = ("127.0.0.1", relay._hub_sock.getsockname()[1])
        stranger.sendto(b"arm-and-take-off", relay_gcs)  # the unfiltered pilot path, from someone else on the network
        stranger.sendto(b"anything", relay_hub)
        pilot.sendto(b"pilot-command", relay_gcs)
        data, _ = fc.recvfrom(4096)
        self.assertEqual(data, b"pilot-command")
        with self.assertRaises(socket.timeout):
            fc.recvfrom(4096)
        status = relay.status()
        self.assertEqual((status["foreign_frames"], status["gcs_frames"]), (2, 1))
        self.assertEqual(status["dropped_by_reason"]["unexpected sender"], 2)

    def test_wildcard_peers_and_guessed_ground_stations_are_refused(self):
        from sentinel.companion.provision import companion_config
        from sentinel.companion.relay import MavlinkRelay

        with self.assertRaises(ValueError):
            MavlinkRelay("udp:127.0.0.1:5760", "udp:127.0.0.1:14551", ["udp:0.0.0.0:14550"])
        self.assertEqual(companion_config(fc="tcp:127.0.0.1:5760", hub_host="192.168.1.10")["gcs"], [])


class ConfigTests(unittest.TestCase):
    def test_swapped_field_of_view_stream_checks_and_safety_settings_are_validated(self):
        from sentinel.config import _validate_drone_operations
        from sentinel.devices import validate_drone_video

        errors = []
        validate_drone_video({"source": {"type": "rtsp"}, "camera": {"hfov_deg": 51, "vfov_deg": 81}, "check_every_s": 5}, "drone.video", errors)
        self.assertTrue(any("must not exceed hfov_deg" in e for e in errors), errors)
        self.assertTrue(any("check_every_s" in e for e in errors), errors)
        errors = []
        validate_drone_video({"source": {"type": "rtsp"}, "camera": {"hfov_deg": 81, "vfov_deg": 51}, "check_every_s": 0}, "drone.video", errors)
        self.assertEqual(errors, [])
        errors = []
        _validate_drone_operations({"safety": {"lost_contact_s": 30, "safe_action_retry_s": 10}}, {}, errors)
        self.assertEqual(errors, ["drone.safety.lost_contact_s must be between 120 and 7200"])


class LockoutTests(unittest.TestCase):
    def test_system_id_lockout_states_its_limit_and_signing_counts_as_a_lockout(self):
        from sentinel.drone import catalog

        lite = catalog.MODEL_INDEX["sentinel_lite"]
        state, detail = catalog.command_lockout(lite, {"MAV_OPTIONS": 1, "MAV_GCS_SYSID": 255}, 245, None)
        self.assertEqual(state, "pass")
        self.assertIn("imitate the pilot's system id", detail)
        state, detail = catalog.command_lockout(lite, {}, 245, None, signed=True)
        self.assertEqual(state, "pass")
        self.assertIn("signs", detail)
        self.assertEqual(catalog.command_lockout(lite, {"MAV_OPTIONS": 0}, 245, None, signed=False)[0], "fail")


def _numpy():
    try:
        import cv2  # noqa: F401
        import numpy
    except ImportError:
        return None
    return numpy


class FakeSource:
    """Frames from a script: 'live' (noisy), 'frozen' (the same picture), 'blank', or None (no frame)."""

    def __init__(self, script, np):
        self.script = list(script)
        self.np = np
        self.rng = np.random.default_rng(1)
        self.base = np.full((90, 160, 3), 90, np.uint8)
        self.base[30:60, 60:100] = 200
        self.frozen = None
        self.closed = False
        self.now = 1_000_000

    def read(self):
        from sentinel.vision import Frame

        if not self.script:
            time.sleep(0.01)
            return None
        kind = self.script.pop(0)
        self.now += 200
        if kind is None:
            return None
        if kind == "blank":
            return Frame(self.np.zeros((90, 160, 3), self.np.uint8), self.now)
        if kind == "frozen":
            if self.frozen is None:
                self.frozen = self.live()
            return Frame(self.frozen.copy(), self.now)
        return Frame(self.live(), self.now)

    def live(self):
        noise = self.rng.integers(-2, 3, size=self.base.shape)
        return self.np.clip(self.base.astype(self.np.int16) + noise, 0, 255).astype(self.np.uint8)

    def close(self):
        self.closed = True


class StubManager:
    def __init__(self, phase="inspecting"):
        from sentinel.drone.base import DroneTelemetry

        self.drone_id = "d1"
        self.site_origin = ORIGIN
        self.request = {"request_id": "insp_1", "target_lat": HOME[0], "target_lon": HOME[1], "target_label": "Shed", "phase": phase}
        self.active = True
        self.attached = []
        telemetry = DroneTelemetry(connected=True, link_ok=True, lat=HOME[0], lon=HOME[1], alt_m=12, gps_fix=3, in_air=True,
                                   telemetry_ms=int(time.time() * 1000))
        self.adapter = SimpleNamespace(telemetry=lambda: telemetry)

    def active_inspection(self):
        return dict(self.request) if self.active else None

    def attach_observations(self, request_id, summary, evidence):
        self.attached.append((summary, list(evidence)))


@unittest.skipIf(_numpy() is None, "OpenCV/numpy not installed")
class DroneVideoReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.np = _numpy()

    def test_quality_monitor_rejects_frozen_and_blank_pictures(self):
        from sentinel.drone.video import VideoQualityMonitor

        source, quality = FakeSource(["live"] * 4 + ["frozen"] * 12 + ["blank"], self.np), VideoQualityMonitor(5)
        verdicts = []
        for _ in range(17):
            frame = source.read()
            verdicts.append(quality.check(frame.image, frame.timestamp_ms)[1] or "ok")
        self.assertEqual(verdicts[:4], ["ok"] * 4, "sensor noise proves a still scene is live")
        self.assertIn("frozen", verdicts[5:16], "an identical picture for 1.5 s is frozen")
        self.assertEqual(verdicts[:verdicts.index("frozen")].count("frozen"), 0)
        self.assertEqual(verdicts[-1], "blank")
        dark = self.np.clip(self.np.full((90, 160, 3), 10, self.np.int16) + self.np.random.default_rng(2).integers(0, 9, (90, 160, 3)), 0, 255)
        self.assertEqual(VideoQualityMonitor(5).check(dark.astype(self.np.uint8), 1)[2], ["very dark"])

    def worker(self, script, manager=None, record_evidence=None, detector=None):
        import tempfile

        from sentinel.drone.video import DroneVideoWorker
        from sentinel.models import Detection

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = manager or StubManager()
        source = FakeSource(script, self.np)
        person = Detection("person", 0.9, (0.4, 0.4, 0.1, 0.2))
        detector = detector or SimpleNamespace(detect=lambda frame: [person])
        worker = DroneVideoWorker(manager, {"source": {"type": "rtsp"}, "fps": 5, "check_every_s": 0}, config={},
                                  media_root=Path(directory.name), record_evidence=record_evidence or (lambda *a: None),
                                  detector_factory=lambda: detector, source_factory=lambda: source)
        return worker, manager, source

    def run_inspection(self, worker, manager, source):
        worker.start()
        self.addCleanup(lambda: (worker.stop(), worker.join(5)))
        self.assertTrue(wait_for(lambda: not source.script, 10))
        time.sleep(0.3)
        manager.active = False
        self.assertTrue(wait_for(lambda: manager.attached and manager.attached[-1][0].get("final"), 5))
        return manager.attached[-1]

    def test_frozen_video_mid_inspection_is_not_coverage(self):
        worker, manager, source = self.worker(["live"] * 6 + ["frozen"] * 20)
        summary, evidence = self.run_inspection(worker, manager, source)
        video = summary["video"]
        self.assertGreater(video["frames_rejected"]["frozen"], 5)
        self.assertEqual(video["coverage"], "partial")
        text = " ".join(summary["text"])
        self.assertIn("not analysed (frozen picture", text)
        self.assertIn("was not observed", text)

    def test_camera_that_never_delivers_a_live_picture_claims_nothing(self):
        nothing = SimpleNamespace(detect=lambda frame: [])
        worker, manager, source = self.worker(["blank"] * 4 + [None] * 5 + ["frozen"] * 12, detector=nothing)
        summary, _ = self.run_inspection(worker, manager, source)
        self.assertEqual(summary["video"]["coverage"], "none")
        self.assertIn("nothing can be said about the area", summary["text"][0])
        self.assertNotIn("did not detect any person, vehicle or animal. This does not prove", summary["text"][0])

    def test_evidence_store_failure_is_contained_and_reported(self):
        import sqlite3

        def broken(*args):
            raise sqlite3.OperationalError("database is locked")

        worker, manager, source = self.worker(["live"] * 8, record_evidence=broken)
        summary, evidence = self.run_inspection(worker, manager, source)
        self.assertEqual(evidence, [], "no empty or unrecorded evidence paths")
        self.assertEqual(summary["video"]["evidence_failures"], 1)
        self.assertIn("could not be saved as evidence", " ".join(summary["text"]))
        self.assertEqual(summary["max_visible"], {"person": 1}, "the observation itself is kept")

    def test_a_crash_mid_inspection_keeps_what_was_already_observed(self):
        calls = {"n": 0}
        from sentinel.models import Detection

        def detect(frame):
            calls["n"] += 1
            if calls["n"] == 5:
                raise RuntimeError("detector crashed")
            return [Detection("person", 0.9, (0.4, 0.4, 0.1, 0.2))]

        worker, manager, source = self.worker(["live"] * 12, detector=SimpleNamespace(detect=detect))
        summary, _ = self.run_inspection(worker, manager, source)
        self.assertEqual(summary["observations"][0]["frames"], 10, "frames before and after the retry are one record")
        self.assertEqual(summary["frames"], 11)

    def test_stale_gimbal_and_no_gps_fix(self):
        from sentinel.drone.observation import CameraModel, pose_from_telemetry

        camera = CameraModel()
        telemetry = DroneTelemetry(lat=HOME[0], lon=HOME[1], alt_m=12, gps_fix=3, gimbal_pitch_deg=-45, gimbal_ms=1000, telemetry_ms=1000)
        self.assertEqual(pose_from_telemetry(telemetry, camera, 1500).attitude_source, "gimbal_reported")
        stale = pose_from_telemetry(telemetry, camera, 4000)
        self.assertEqual((stale.attitude_source, stale.pitch_deg), ("configured", -90.0), "a 3 s old gimbal angle is not used")
        telemetry.gps_fix = 1
        self.assertIsNone(pose_from_telemetry(telemetry, camera, 1500))

    def test_slewing_gimbal_gives_no_position(self):
        from sentinel.drone.observation import CameraModel, CameraPose, InspectionObserver
        from sentinel.models import Detection

        observer = InspectionObserver("i", HOME, "Shed", CameraModel(), ORIGIN)
        person = Detection("person", 0.9, (0.45, 0.45, 0.06, 0.08))
        for index, pitch in enumerate([-90, -90, -60, -30]):  # 30 degrees in 0.2 s
            pose = CameraPose(lat=HOME[0], lon=HOME[1], height_m=12, heading_deg=0, pitch_deg=pitch, attitude_source="gimbal_reported")
            observer.update([person], 1000 + 200 * index, pose, on_station=True)
        reasons = next(iter(observer.observations.values())).no_estimate_reasons
        self.assertEqual(reasons.get("camera angle changing"), 2)

    def test_unreachable_camera_is_reported_before_the_flight(self):
        from sentinel.drone.video import DroneVideoWorker

        def unreachable():
            raise ConnectionRefusedError("rtsp://192.168.144.25:8554 refused")

        manager = StubManager()
        manager.active = False
        worker = DroneVideoWorker(manager, {"source": {"type": "rtsp"}, "fps": 5, "check_every_s": 30}, config={}, media_root=Path("."),
                                  record_evidence=lambda *a: None, source_factory=unreachable)
        worker._idle()
        self.assertEqual(worker.state, "unreachable")
        self.assertIn("cannot open the stream", worker.detail)
        frozen = FakeSource(["frozen"] * 40, self.np)
        worker.source_factory = lambda: frozen
        result = worker.check_stream(timeout_s=1.0)
        self.assertFalse(result["ok"])
        self.assertIn("frozen", result["detail"])


class TroubleshootTests(unittest.TestCase):
    def test_health_conditions_get_causes_steps_and_one_next_step(self):
        import inspect
        import re

        from sentinel.drone import catalog, flight_health
        from sentinel.drone.troubleshoot import HEALTH_GUIDE, diagnose

        lite = catalog.MODEL_INDEX["sentinel_lite"]
        status = {"drone_id": "d1", "physical": True, "readiness": {"checks": [], "blockers": []},
                  "health": {"conditions": [{"id": "gps_no_fix", "severity": "critical", "text": "GPS lost in flight", "evidence": {"fix": 1}},
                                            {"id": "home_mismatch", "severity": "warn", "text": "Home far from configured", "evidence": {}}]},
                  "video": {"state": "frozen", "detail": "drone camera picture frozen"}}
        result = diagnose(status, lite)
        self.assertEqual([f["check"] for f in result["findings"]], ["health:gps_no_fix", "health:home_mismatch", "video:frozen"])
        self.assertEqual(result["next_step"]["check"], "health:gps_no_fix")
        self.assertIn("open sky", result["next_step"]["step"])
        self.assertEqual(result["next_step"]["remaining_steps"], 2)
        monitor_ids = set(re.findall(r'add\("([a-z_]+)"', inspect.getsource(flight_health)))
        self.assertGreater(len(monitor_ids), 20)
        self.assertEqual(monitor_ids - set(HEALTH_GUIDE), set(), "every flight-health condition has guidance")


class FlightHealthTests(unittest.TestCase):
    def telemetry(self, **changes):
        values = dict(connected=True, link_ok=True, armed=False, in_air=False, battery_pct=90.0, battery_ms=1000, lat=HOME[0], lon=HOME[1],
                      telemetry_ms=1000, gps_fix=3, satellites=14, hdop=0.8, gps_ms=1000, ekf_ok=True, ekf_ms=1000, home_lat=HOME[0],
                      home_lon=HOME[1], last_heartbeat_ms=1000)
        return DroneTelemetry(**(values | changes))

    def ids(self, conditions):
        return {c["id"]: c["severity"] for c in conditions}

    def test_healthy_aircraft_has_no_conditions(self):
        monitor = FlightHealthMonitor(configured_home=HOME, endurance_min_est=15)
        self.assertEqual(monitor.update(self.telemetry(), 1500), [])
        self.assertEqual(monitor.summary(self.telemetry(), 1500)["return_home_possible"], True)

    def test_stale_values_are_reported_with_their_age(self):
        monitor = FlightHealthMonitor()
        conditions = self.ids(monitor.update(self.telemetry(in_air=True, armed=True), 20_000))
        self.assertEqual(conditions, {"position_stale": "critical", "gps_stale": "critical", "battery_stale": "critical", "ekf_stale": "warn"})
        text = next(c["text"] for c in monitor.conditions if c["id"] == "battery_stale")
        self.assertIn("19 s ago", text)
        self.assertIn("not current", text)

    def test_gps_loss_in_flight_says_return_home_may_not_be_possible(self):
        monitor = FlightHealthMonitor()
        lost = self.telemetry(in_air=True, armed=True, gps_fix=1, satellites=3, ekf_ok=False)
        conditions = self.ids(monitor.update(lost, 1200))
        self.assertEqual((conditions["gps_no_fix"], conditions["ekf_unhealthy"]), ("critical", "critical"))
        self.assertIn("may not be able to return home", next(c["text"] for c in monitor.conditions if c["id"] == "gps_no_fix"))
        possible, why = monitor.return_home_possible(lost, 1200)
        self.assertFalse(possible)
        self.assertEqual(why, "no GPS fix")
        self.assertEqual(self.ids(monitor.update(self.telemetry(satellites=4, hdop=3.1), 1200)), {"gps_degraded": "warn"})

    def test_home_problems(self):
        far = map_point_to_geo(ORIGIN, 80, 4)
        monitor = FlightHealthMonitor(configured_home=HOME)
        self.assertEqual(self.ids(monitor.update(self.telemetry(home_lat=far[0], home_lon=far[1]), 1200)), {"home_mismatch": "warn"})
        self.assertEqual(self.ids(monitor.update(self.telemetry(home_lat=None, home_lon=None), 1200)), {"home_unknown": "warn"})
        self.assertEqual(self.ids(monitor.update(self.telemetry(lat=None, lon=None, position_rejected=4), 1200))["position_unknown"], "warn")

    def test_link_flapping_restart_and_a_second_autopilot(self):
        monitor = FlightHealthMonitor()
        for second in range(3):
            monitor.update(self.telemetry(last_heartbeat_ms=second * 4000), second * 4000)
            monitor.update(self.telemetry(link_ok=False, last_heartbeat_ms=second * 4000), second * 4000 + 2000)
        conditions = self.ids(monitor.update(self.telemetry(last_heartbeat_ms=12_000, battery_ms=12_000, telemetry_ms=12_000, gps_ms=12_000,
                                                             ekf_ms=12_000, fc_reboots=1, other_vehicles=["7/1"]), 12_000))
        self.assertEqual(conditions, {"link_unstable": "warn", "fc_restarted": "warn", "other_vehicle": "warn"})

    def flight(self, monitor, pcts, step_ms=10_000, **changes):
        now = 0
        for pct in pcts:
            now += step_ms
            stamp = dict(battery_pct=pct, battery_ms=now, telemetry_ms=now, gps_ms=now, ekf_ms=now, last_heartbeat_ms=now)
            conditions = monitor.update(self.telemetry(in_air=True, armed=True, alt_m=12, **(stamp | changes)), now)
        return self.ids(conditions)

    def test_drain_against_the_model_estimate_and_return_now(self):
        # A 15 min hover estimate -> 6.7 %/min. Observed: 2 % every 10 s = 12 %/min.
        far = map_point_to_geo(ORIGIN, 1000, 4)
        monitor = FlightHealthMonitor(endurance_min_est=15, configured_home=HOME, cruise_mps=5)
        conditions = self.flight(monitor, [90, 88, 86, 84, 82, 80], lat=far[0], lon=far[1])
        self.assertEqual(conditions["battery_fast_drain"], "warn")
        drain = monitor.drain()
        self.assertEqual((drain["observed_pct_per_min"], drain["expected_pct_per_min"]), (12.0, 6.67))
        # ~1 km out: about 3.9 min home; 50 % to the reserve at 12 %/min is 4.2 min, inside the 1.5x margin.
        self.assertEqual(conditions["battery_return_now"], "critical")
        normal = FlightHealthMonitor(endurance_min_est=15, configured_home=HOME)
        self.assertEqual(self.flight(normal, [90, 89, 88, 87, 86, 85]), {})

    def test_a_battery_level_that_never_changes_in_flight_looks_stuck(self):
        monitor = FlightHealthMonitor(limits=HealthLimits(battery_frozen_s=60))
        self.assertEqual(self.flight(monitor, [80] * 9), {"battery_frozen": "warn"})

    def test_flight_controller_battery_verdict_wind_and_link_delay(self):
        monitor = FlightHealthMonitor(max_wind_mps=6)
        now = 1000
        flying = dict(in_air=True, armed=True, battery_ms=now, telemetry_ms=now, gps_ms=now, ekf_ms=now, last_heartbeat_ms=now)
        conditions = self.ids(monitor.update(self.telemetry(battery_state="low", battery_voltage=10.4, wind_mps=9.5, link_delay_ms=2500, **flying), now))
        self.assertEqual(conditions, {"battery_failsafe": "critical", "wind_over_limit": "warn", "link_delayed": "critical"})
        text = next(c["text"] for c in monitor.conditions if c["id"] == "battery_failsafe")
        self.assertIn("10.40 V and 90%", text, "the FC's verdict is shown even when the percentage looks fine")

    def test_an_exact_zero_wind_vector_is_no_estimate_not_calm_air(self):
        t = DroneTelemetry(connected=True, link_ok=True, in_air=True, armed=True)
        apply_mavlink_message(t, message("WIND", direction=0.0, speed=7.24, speed_z=0.1), 1000)
        self.assertEqual(t.wind_mps, 7.2)
        apply_mavlink_message(t, message("WIND", direction=-180.0, speed=0.0, speed_z=0.0), 2000)
        self.assertIsNone(t.wind_mps, "ArduCopter without drag fusion streams zeros: Sentinel must not show 0 m/s")
        apply_mavlink_message(t, message("WIND", direction=90.0, speed=0.04, speed_z=0.0), 3000)
        self.assertEqual(t.wind_mps, 0.0)

    def test_the_flight_controllers_fence_verdict_is_reported(self):
        t = DroneTelemetry(connected=True, link_ok=True, in_air=True, armed=True)
        apply_mavlink_message(t, message("FENCE_STATUS", breach_status=1, breach_count=2, breach_type=2, breach_time=0), 1000)
        self.assertEqual((t.fence_breach, t.fence_breaches), ("max_alt", 2))
        monitor = FlightHealthMonitor()
        now = 1000
        condition = next(c for c in monitor.update(self.telemetry(in_air=True, armed=True, fence_breach="max_alt", battery_ms=now, telemetry_ms=now,
                                                                   gps_ms=now, ekf_ms=now, last_heartbeat_ms=now), now) if c["id"] == "fence_breach")
        self.assertEqual(condition["severity"], "critical")
        self.assertIn("above the altitude ceiling", condition["text"])
        apply_mavlink_message(t, message("FENCE_STATUS", breach_status=0, breach_count=2, breach_type=0, breach_time=0), 2000)
        self.assertEqual(t.fence_breach, "")

    def test_clock_offset_measures_added_delay_and_resets_on_restart(self):
        from sentinel.drone.mavlink import _ClockOffset

        t, clock = DroneTelemetry(), _ClockOffset()
        for second in range(5):
            apply_mavlink_message(t, message("ATTITUDE", roll=0, pitch=0, time_boot_ms=100_000 + second * 1000), 50_000 + second * 1000, clock)
        self.assertEqual(t.link_delay_ms, 0)
        apply_mavlink_message(t, message("ATTITUDE", roll=0, pitch=0, time_boot_ms=105_000), 57_500, clock)
        self.assertEqual(t.link_delay_ms, 2500)
        for index, boot in enumerate((2_000, 2_250, 2_500)):
            apply_mavlink_message(t, message("ATTITUDE", roll=0, pitch=0, time_boot_ms=boot), 58_000 + index * 250, clock)
            self.assertEqual(t.fc_reboots, 1 if index == 2 else 0, "a restart is believed on the third message of the new clock")
        self.assertEqual((t.boot_ms, t.link_delay_ms), (2_500, 0))

    def test_late_packets_after_a_delay_clears_are_not_a_restart(self):
        """Regression (found in SITL): frames queued behind a 2.5 s delay arrived after fresh ones; at
        simulator speed-up 3 their boot times were 7.5 s older and each one looked like a restart."""
        from sentinel.drone.mavlink import _ClockOffset

        t, clock = DroneTelemetry(), _ClockOffset()
        for boot, arrival in ((500_000, 1000), (507_500, 1250), (500_250, 1300), (507_750, 1500), (500_500, 1550), (508_000, 1750)):
            apply_mavlink_message(t, message("SYSTEM_TIME", time_boot_ms=boot), arrival, clock)
        self.assertEqual((t.fc_reboots, t.boot_ms), (0, 508_000))

    def test_unknown_battery_and_disconnected(self):
        monitor = FlightHealthMonitor()
        self.assertEqual(self.ids(monitor.update(self.telemetry(battery_pct=None, battery_rejected=3), 1200)), {"battery_unknown": "warn"})
        self.assertEqual(monitor.update(self.telemetry(connected=False), 1200), [])
        lost = monitor.update(self.telemetry(link_ok=False, in_air=True, last_heartbeat_ms=1000), 9000)
        self.assertEqual(self.ids(lost), {"link_lost": "critical"}, "nothing else is judged from stale data")
        self.assertIn("8 s", lost[0]["text"])


if __name__ == "__main__":
    unittest.main()
