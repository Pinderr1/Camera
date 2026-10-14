"""Unit tests for the reliability building blocks: camera health states,
outages and coverage notifications, worker supervision (backoff, restart
budget, stalls), the storage guard, run state, and background-install helpers.
Everything here runs on fake clocks and fake workers: no cameras, no threads."""

import copy
import json
import os
import tempfile
import time
import unittest
import xml.dom.minidom
from collections import namedtuple
from pathlib import Path

from sentinel.config import validate_config
from sentinel.daemon import RunState, downtime, install_commands, load_env_file, systemd_unit, task_xml
from sentinel.health import HealthMonitor, HealthSettings, camera_state
from sentinel.incidents import DISCLAIMER
from sentinel.storage_guard import StorageGuard, StorageSettings
from sentinel.supervisor import SupervisorSettings, WorkerSupervisor

from sentinel_helpers import BASE_CONFIG

T0 = 1_760_000_000_000
SETTINGS = HealthSettings(frame_timeout_s=10, frozen_after_s=60, startup_grace_s=30, notify_after_s=120)


def status(**fields):
    return {"camera_id": "cam_a", "worker_state": "running", "worker_started_ms": T0, "last_frame_ms": T0 + 1000, "last_inference_ms": T0 + 1000} | fields


class CameraStateTests(unittest.TestCase):
    def test_states_in_priority_order(self):
        now = T0 + 5000
        self.assertEqual(camera_state(status(), now, SETTINGS)[0], "monitoring")
        self.assertEqual(camera_state(status(last_frame_ms=0), now, SETTINGS)[0], "starting")
        self.assertEqual(camera_state(status(last_frame_ms=0), T0 + 31_000, SETTINGS)[0], "offline")
        self.assertEqual(camera_state(status(error="camera unreachable"), T0 + 12_000, SETTINGS), ("offline", "camera unreachable"))
        self.assertEqual(camera_state(status(detector_state="failed", detector_error="GPU lost"), now, SETTINGS), ("ai_failed", "GPU lost"))
        self.assertEqual(camera_state(status(detector_state="fallback"), now, SETTINGS)[0], "monitoring")
        self.assertEqual(camera_state(status(worker_state="restarting", worker_error="boom"), now, SETTINGS), ("restarting", "boom"))
        self.assertEqual(camera_state(status(worker_state="failed"), now, SETTINGS)[0], "failed")
        self.assertEqual(camera_state({"camera_id": "cam_a"}, now, SETTINGS)[0], "not_running")

    def test_frozen_needs_positive_evidence_and_a_still_clean_scene_is_only_unverified(self):
        now = T0 + 100_000
        live = dict(last_frame_ms=now, last_inference_ms=now)
        # Stream timestamps stopped advancing: the decoder repeats one frame.
        self.assertEqual(camera_state(status(**live, pts_ms=500.0, pts_stalled_since_ms=now - 61_000), now, SETTINGS)[0], "frozen")
        # Picture went still right after being noisy: a frozen feed, even with a ticking clock overlay.
        frozen = camera_state(status(**live, content={"static_since_ms": now - 61_000, "static_kind": "overlay", "noisy_before_static": True}), now, SETTINGS)
        self.assertEqual(frozen[0], "frozen")
        self.assertIn("clock", frozen[1])
        # Still and noise-free all along: cannot be told apart from a frozen feed -> unverified, never frozen.
        still = camera_state(status(**live, content={"static_since_ms": now - 600_000, "static_kind": "identical", "noisy_before_static": False}), now, SETTINGS)
        self.assertEqual(still[0], "unverified")
        self.assertIn("cannot be told apart", still[1])
        # Short still periods are normal.
        self.assertEqual(camera_state(status(**live, content={"static_since_ms": now - 10_000, "noisy_before_static": True}), now, SETTINGS)[0], "monitoring")

    def test_inference_heartbeat_is_judged_separately_from_frames(self):
        now = T0 + 100_000
        state, reason = camera_state(status(last_frame_ms=now, last_inference_ms=now - 20_000), now, SETTINGS)
        self.assertEqual(state, "ai_failed")
        self.assertIn("no detection has completed for 20 s", reason)

    def test_frames_from_before_a_restart_do_not_count(self):
        restarted = status(worker_started_ms=T0 + 60_000, last_frame_ms=T0 + 59_000)
        self.assertEqual(camera_state(restarted, T0 + 61_000, SETTINGS)[0], "starting")

    def test_legacy_offline_key_maps_to_notify_delay(self):
        self.assertEqual(HealthSettings.from_dict({"camera_offline_after_s": 45}).notify_after_s, 45)
        self.assertEqual(HealthSettings.from_dict({"camera_offline_after_s": 45, "notify_after_s": 90}).notify_after_s, 90)


class ContentMonitorTests(unittest.TestCase):
    """Synthetic 320x180 frames: what a live sensor, a still clean scene, and the
    two kinds of frozen feed look like to `ContentMonitor`."""

    def setUp(self):
        import numpy as np

        self.np = np
        self.rng = np.random.default_rng(3)
        self.base = np.full((180, 320, 3), 90, dtype=np.uint8)
        self.base[100:, :] = 60

    def frame(self, *, noise=0, clock=None, base=None):
        image = (self.base if base is None else base).copy()
        if noise:
            image = (image.astype(self.np.int16) + self.rng.integers(-noise, noise + 1, image.shape)).clip(0, 255).astype(self.np.uint8)
        if clock is not None:  # a recorder's on-screen clock, top-right
            image[4:16, 270:316] = 20
            image[6:14, 272 + (clock % 40):276 + (clock % 40)] = 230
        return image

    def run_frames(self, frames, start=0):
        from sentinel.vision import ContentMonitor

        monitor = getattr(self, "monitor", None) or ContentMonitor()
        self.monitor = monitor
        result = None
        for index, image in enumerate(frames):
            result = monitor.update(image, T0 + (start + index) * 200)
        return result

    def test_a_noisy_static_scene_is_live(self):
        result = self.run_frames([self.frame(noise=3) for _ in range(30)])
        self.assertEqual(result["kind"], "noise")
        self.assertEqual(result["static_since_ms"], 0)

    def test_a_clean_still_scene_is_static_without_noise_evidence(self):
        result = self.run_frames([self.frame(clock=i) for i in range(30)])
        self.assertEqual((result["static_kind"], result["noisy_before_static"]), ("overlay", False))
        self.assertGreater(result["static_since_ms"], 0)

    def test_noisy_feed_that_freezes_is_flagged_even_with_a_ticking_clock(self):
        self.run_frames([self.frame(noise=3, clock=i) for i in range(30)])
        frozen = self.frame(noise=3)
        result = self.run_frames([frozen.copy() for _ in range(5)] + [self.frame(base=frozen, clock=i) for i in range(10)], start=30)
        self.assertTrue(result["noisy_before_static"])
        self.assertEqual(result["static_kind"], "overlay")

    def test_movement_in_a_clean_scene_is_not_mistaken_for_noise(self):
        frames = []
        for i in range(20):
            image = self.frame()
            image[60:120, 10 + i * 10:40 + i * 10] = 200  # a person walking across
            frames.append(image)
        frames += [self.frame() for _ in range(10)]
        result = self.run_frames(frames)
        self.assertFalse(result["noisy_before_static"], "movement then stillness must not look like a feed losing its noise")


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.outages, self.events, self.notifications = {}, [], []
        self.monitor = HealthMonitor(
            HealthSettings(notify_after_s=120),
            save_outage=lambda outage: self.outages.__setitem__(outage.outage_id, outage.as_dict()),
            record_event=lambda *args: self.events.append(args),
            notify=self.notifications.append,
            camera_name=lambda camera_id: {"cam_a": "Front door", "cam_b": "Yard"}.get(camera_id, camera_id),
        )
        self.cameras = {"cam_a": status(camera_id="cam_a"), "cam_b": status(camera_id="cam_b")}

    def run_until(self, until_ms, step_ms=1000, **changes):
        for camera_id, fields in changes.items():
            self.cameras[camera_id].update(fields)
        now = self.now
        while now <= until_ms:
            for camera in self.cameras.values():
                if camera.get("_live"):
                    camera["last_frame_ms"] = camera["last_inference_ms"] = now
            self.monitor.evaluate(self.cameras, now)
            now += step_ms
        self.now = now

    def test_outage_is_recorded_notified_once_and_recovery_is_reported(self):
        for camera in self.cameras.values():
            camera["_live"] = True
        self.now = T0 + 1000
        self.run_until(T0 + 10_000)
        self.assertEqual(self.monitor.coverage(self.cameras)["state"], "full")
        self.cameras["cam_a"].update(_live=False, error="camera unreachable")
        self.run_until(T0 + 60_000)
        self.assertEqual(self.cameras["cam_a"]["health"]["state"], "offline")
        self.assertFalse(self.cameras["cam_a"]["health"]["monitoring"])
        self.assertEqual(self.monitor.coverage(self.cameras)["state"], "degraded")
        self.assertEqual([event[1] for event in self.events], ["camera_offline"])
        self.assertEqual(self.notifications, [], "short outages are not pushed")
        self.run_until(T0 + 300_000)
        self.assertEqual(len(self.notifications), 1)
        notification = self.notifications[0]
        self.assertEqual((notification.kind, notification.title), ("health", "Camera not monitoring: Front door"))
        self.assertIn("camera unreachable", notification.message)
        self.assertTrue(notification.message.endswith(DISCLAIMER))
        self.cameras["cam_a"].update(_live=True, error="")
        self.run_until(T0 + 305_000)
        self.assertEqual(self.cameras["cam_a"]["health"]["state"], "monitoring")
        self.assertEqual(self.notifications[-1].title, "Monitoring restored: Front door")
        outage = next(iter(self.outages.values()))
        self.assertEqual((outage["kind"], outage["camera_id"], outage["notified"]), ("offline", "cam_a", True))
        self.assertGreater(outage["ended_ms"], outage["started_ms"])
        self.assertEqual(self.events[-1][1], "camera_online")

    def test_brief_dropout_is_neither_an_outage_nor_a_notification(self):
        for camera in self.cameras.values():
            camera["_live"] = True
        self.now = T0 + 1000
        self.run_until(T0 + 5000)
        self.cameras["cam_a"]["_live"] = False
        self.run_until(T0 + 12_000)  # 7 s without frames: under frame_timeout_s
        self.cameras["cam_a"]["_live"] = True
        self.run_until(T0 + 20_000)
        self.assertEqual((self.outages, self.notifications), ({}, []))

    def test_simultaneous_outages_are_grouped_into_one_notification(self):
        self.now = T0 + 1000
        self.run_until(T0 + 200_000)  # no camera ever delivers a frame after start
        self.assertEqual(self.monitor.coverage(self.cameras)["state"], "none")
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.notifications[0].title, "2 cameras not monitoring")
        self.assertEqual(set(self.notifications[0].camera_ids), {"cam_a", "cam_b"})

    def test_state_changes_within_one_outage_extend_it(self):
        self.cameras["cam_a"]["_live"] = True
        self.cameras["cam_b"]["_live"] = True
        self.now = T0 + 1000
        self.run_until(T0 + 3000)
        self.run_until(T0 + 10_000, cam_a={"worker_state": "restarting", "worker_error": "RuntimeError: decoder crashed"})
        self.run_until(T0 + 20_000, cam_a={"worker_state": "failed"})
        outage = next(iter(self.outages.values()))
        self.assertEqual(outage["states"], ["restarting", "failed"])
        self.run_until(T0 + 200_000)
        self.assertEqual(self.notifications[0].severity, "high", "a camera that gave up restarting is urgent")

    def test_notification_rate_limit_holds_back_and_counts(self):
        self.monitor.settings.max_notifications_per_hour = 2
        for index in range(4):
            self.monitor.announce(f"n{index}", ["line"], "medium", T0 + index, f"r{index}", ())
        self.assertEqual(len(self.notifications), 2)
        self.monitor.announce("later", ["line"], "medium", T0 + 3_700_000, "r9", ())
        self.assertIn("2 other health update(s) were held back", self.notifications[-1].message)

    def test_components_open_and_close_outages(self):
        self.monitor.set_component("storage", "failed", "disk nearly full", T0)
        self.assertEqual(self.monitor.coverage({})["components"]["storage"]["state"], "failed")
        self.monitor.evaluate({}, T0 + 130_000)
        self.assertEqual(self.notifications[0].title, "Recording storage needs attention")
        self.monitor.set_component("storage", "ok", "", T0 + 140_000)
        self.assertEqual(self.notifications[-1].title, "Back to normal: Recording storage")
        self.monitor.set_component("storage", "degraded", "space low", T0 + 150_000, notify=False)
        self.monitor.evaluate({}, T0 + 500_000)
        self.assertEqual(len(self.notifications), 2, "notify=False components are shown but not pushed")

    def test_coverage_is_degraded_when_a_component_fails_even_with_all_cameras_live(self):
        for camera in self.cameras.values():
            camera["_live"] = True
        self.now = T0 + 1000
        self.run_until(T0 + 2000)
        self.monitor.set_component("database", "degraded", "disk full", T0 + 2000)
        self.assertEqual(self.monitor.coverage(self.cameras)["state"], "degraded")

    def test_disabled_cameras_session_is_not_an_outage(self):
        cameras = {"cam_a": {"camera_id": "cam_a"}}
        self.monitor.evaluate(cameras, T0 + 1_000_000)
        self.assertEqual(self.monitor.coverage(cameras)["state"], "not_running")
        self.assertEqual((self.outages, self.notifications), ({}, []))

    def test_slow_processing_is_a_warning_not_an_outage(self):
        self.monitor.settings.slow_after_s = 10
        self.cameras["cam_a"].update(_live=True, fps=1.0, target_fps=5)
        self.cameras["cam_b"]["_live"] = True
        self.now = T0 + 1000
        self.run_until(T0 + 20_000)
        health = self.cameras["cam_a"]["health"]
        self.assertEqual((health["state"], health["warnings"]), ("monitoring", ["slow_processing"]))


class FakeWorker:
    def __init__(self, clock):
        self.clock = clock
        self.alive = False
        self.failure = None
        self.phase = "running"
        self.started_at = self.heartbeat = 0.0
        self.stopped = False

    def start(self):
        self.alive = True
        self.started_at = self.heartbeat = self.clock.now

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.workers = []
        self.statuses = []
        self.fatal = []

        def factory(camera):
            worker = FakeWorker(self.clock)
            self.workers.append(worker)
            return worker

        self.supervisor = WorkerSupervisor(
            SupervisorSettings(stall_after_s=30, backoff_initial_s=5, backoff_max_s=40, max_restarts=3, window_s=3600, cooldown_s=1800, max_stuck_threads=1),
            factory, lambda camera_id, fields: self.statuses.append(fields), on_fatal=self.fatal.append, clock=self.clock,
        )
        self.supervisor.add({"id": "cam_a"})
        self.supervisor.start()

    def crash(self, message="RuntimeError: boom"):
        worker = self.workers[-1]
        worker.alive, worker.failure = False, message
        self.supervisor.check()

    def advance(self, seconds):
        self.clock.now += seconds
        self.supervisor.check()

    def test_crash_restarts_with_exponential_backoff_then_circuit_breaker(self):
        delays = []
        for _ in range(3):
            self.crash()
            self.assertEqual(self.statuses[-1]["worker_state"], "restarting")
            self.assertEqual(self.statuses[-1]["worker_error"], "RuntimeError: boom")
            before = len(self.workers)
            waited = 0
            while len(self.workers) == before:
                self.advance(1)
                waited += 1
            delays.append(waited)
        self.assertEqual(delays, [5, 10, 20])
        self.crash()  # 4th crash within the hour: budget exhausted
        self.assertEqual(self.statuses[-1]["worker_state"], "failed")
        before = len(self.workers)
        self.advance(1700)
        self.assertEqual(len(self.workers), before, "no restart loop while the circuit is open")
        self.advance(101)
        self.assertEqual(len(self.workers), before + 1, "one trial restart after the cooldown")
        self.assertEqual(self.supervisor.status("cam_a")["restarts_total"], 4)

    def test_restart_budget_recovers_after_the_window(self):
        for _ in range(3):
            self.crash()
            self.advance(60)
        self.advance(3600)
        self.crash()
        self.assertEqual(self.statuses[-1]["worker_state"], "restarting")
        self.assertEqual(self.statuses[-1]["restarts_recent"], 1)

    def test_stalled_worker_is_abandoned_and_replaced(self):
        stuck = self.workers[-1]
        self.advance(29)
        self.assertEqual(len(self.workers), 1)
        self.advance(2)
        self.assertTrue(stuck.stopped)
        self.assertIn("stalled", self.statuses[-1]["worker_error"])
        self.assertEqual(self.supervisor.abandoned, [stuck])
        self.advance(5)
        self.assertEqual(len(self.workers), 2)
        stuck.alive = False  # the hung call finally returned
        self.advance(1)
        self.assertEqual(self.supervisor.abandoned, [])
        self.assertEqual(self.fatal, [])

    def test_too_many_stuck_threads_is_fatal(self):
        for _ in range(2):
            self.advance(31)
            self.advance(10)
        self.assertEqual(len(self.supervisor.abandoned), 2)
        self.assertEqual(len(self.fatal), 1)
        self.assertIn("stuck", self.fatal[0])

    def test_startup_timeout_covers_slow_model_loading(self):
        worker = self.workers[-1]
        worker.phase = "starting"
        self.advance(290)
        self.assertEqual(len(self.supervisor.abandoned), 0, "loading a model is not a stall")
        self.advance(20)
        self.assertEqual(self.supervisor.abandoned, [worker])

    def test_manual_restart_resets_an_open_circuit(self):
        for _ in range(4):
            self.crash()
            self.advance(60)
        self.assertEqual(self.supervisor.status("cam_a")["worker_state"], "failed")
        self.supervisor.restart("cam_a")
        self.assertEqual(self.supervisor.status("cam_a")["worker_state"], "running")
        self.assertEqual(self.supervisor.status("cam_a")["restarts_recent"], 0)

    def test_stopped_supervisor_does_not_restart(self):
        self.supervisor.stop()
        self.workers[-1].alive = False
        self.advance(100)
        self.assertEqual(len(self.workers), 1)


Usage = namedtuple("Usage", "total used free")


class StorageGuardTests(unittest.TestCase):
    MB = 2**20

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.media = Path(self.directory.name) / "media"
        self.protected = set()
        self.base_free_mb = 1000.0
        self.files = []
        for index in range(5):
            path = self.media / "clips" / "cam_a" / f"clip{index}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 10)
            os.utime(path, (time.time() - 1000 + index, time.time() - 1000 + index))  # clip0 is the oldest
            self.files.append(path)
        self.protected.add("clips/cam_a/clip0.mp4")
        self.guard = StorageGuard(StorageSettings(min_free_mb=100, min_free_pct=1, resume_margin=1.5), self.media, lambda: self.protected, disk_usage=self.usage)

    def tearDown(self):
        self.directory.cleanup()

    def usage(self, _path):
        deleted = sum(1 for path in self.files if not path.exists())
        free = (self.base_free_mb + 50 * deleted) * self.MB  # each clip "occupies" 50 MB
        return Usage(5000 * self.MB, 5000 * self.MB - free, free)

    def test_plenty_of_space_changes_nothing(self):
        status = self.guard.check()
        self.assertFalse(status["low"] or status["recording_paused"])
        self.assertTrue(all(path.exists() for path in self.files))

    def test_low_space_deletes_oldest_unprotected_first(self):
        self.base_free_mb = 60  # below 100: needs 150 to be comfortable, i.e. two clips
        status = self.guard.check()
        self.assertEqual([path.exists() for path in self.files], [True, False, False, True, True])
        self.assertEqual((status["purged_now"], status["low"], status["recording_paused"]), (2, False, False))

    def test_recording_pauses_when_nothing_more_can_be_deleted_and_resumes_with_hysteresis(self):
        self.protected |= {f"clips/cam_a/clip{index}.mp4" for index in range(5)}
        self.base_free_mb = 20
        self.assertTrue(self.guard.check()["recording_paused"])
        self.assertTrue(all(path.exists() for path in self.files), "protected recordings are never deleted for space")
        self.base_free_mb = 120  # above the threshold but below threshold x 1.5
        self.assertTrue(self.guard.check()["recording_paused"])
        self.base_free_mb = 200
        self.assertFalse(self.guard.check()["recording_paused"])

    def test_media_quota(self):
        self.guard.settings.max_media_gb = 30 / 2**30 * 1  # 30 bytes: the five 10-byte clips exceed it
        self.guard.check()
        remaining = [path for path in self.files if path.exists()]
        self.assertLessEqual(sum(path.stat().st_size for path in remaining), 27)
        self.assertTrue(self.files[0].exists(), "quota purges skip protected media too")


class RunStateTests(unittest.TestCase):
    def test_clean_and_unclean_stops_are_told_apart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory)
            self.assertIsNone(state.begin())
            state.end()
            previous = RunState(directory).begin()
            gap = downtime(previous, previous["stopped_ms"] + 5000)
            self.assertEqual((gap["clean"], gap["ended_ms"] - gap["started_ms"]), (True, 5000))
            crashed = {"pid": 1, "started_ms": T0, "heartbeat_ms": T0 + 50_000, "clean": False}
            self.assertEqual(downtime(crashed, T0 + 80_000), {"started_ms": T0 + 50_000, "ended_ms": T0 + 80_000, "clean": False})
            self.assertIsNone(downtime(None, T0))

    def test_another_live_instance_is_detected(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
            try:
                now = int(time.time() * 1000)
                Path(directory, "runstate.json").write_text(json.dumps({"pid": child.pid, "heartbeat_ms": now, "clean": False}))
                self.assertEqual(RunState(directory).other_instance()["pid"], child.pid)
                Path(directory, "runstate.json").write_text(json.dumps({"pid": child.pid, "heartbeat_ms": now - 120_000, "clean": False}))
                self.assertIsNone(RunState(directory).other_instance(), "a stale heartbeat is not a running instance")
            finally:
                child.kill()
                child.wait()


class BackgroundInstallTests(unittest.TestCase):
    def test_task_definition_runs_forever_restarts_and_holds_no_password(self):
        xml_text = task_xml(python=r"C:\Python\pythonw.exe", arguments='"x.py" run --config "c.json"', workdir=r"C:\repo", user=r"PC\me", trigger="startup")
        document = xml.dom.minidom.parseString(xml_text.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode())
        text = lambda tag: document.getElementsByTagName(tag)[0].firstChild.data  # noqa: E731
        self.assertEqual(text("ExecutionTimeLimit"), "PT0S")
        self.assertEqual(text("LogonType"), "Password")
        self.assertEqual(text("MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(text("DisallowStartIfOnBatteries"), "false")
        self.assertTrue(document.getElementsByTagName("BootTrigger"))
        self.assertTrue(document.getElementsByTagName("RestartOnFailure"))
        logon = task_xml(python="p", arguments="a", workdir="w", user=r"PC\me", trigger="logon")
        self.assertIn("<LogonTrigger>", logon)
        self.assertIn("InteractiveToken", logon)

    def test_install_command_prompts_for_the_password_instead_of_taking_it(self):
        xml_text, commands = install_commands(config="config/sentinel.example.json", trigger="startup", user=r"PC\me", env_file=None,
                                              xml_path=Path("task.xml"), python="pythonw.exe")
        self.assertIn("sentinel_launcher.py", xml_text)
        self.assertIn(" run --config ", xml_text)
        self.assertEqual(commands[0][-2:], ["/RP", "*"])

    def test_systemd_unit_restarts_on_failure_but_not_on_config_errors(self):
        unit = systemd_unit(config="config/sentinel.example.json", user="sentinel", env_file="/etc/sentinel.env")
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartPreventExitStatus=78", unit)
        self.assertIn("--exit-on-fatal", unit)
        self.assertIn("EnvironmentFile=", unit)

    def test_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sentinel.env"
            path.write_text("# secrets\nSENTINEL_API_TOKEN='abc=123'\n\nEMPTY=\n")
            self.assertEqual(load_env_file(path), {"SENTINEL_API_TOKEN": "abc=123", "EMPTY": ""})


class ReliabilityConfigValidationTests(unittest.TestCase):
    def config(self, **overrides):
        config = copy.deepcopy(BASE_CONFIG) | overrides
        return config

    def test_valid_settings_pass(self):
        config = self.config(
            health={"frame_timeout_s": 5, "slow_ratio": 0.5, "supervisor": {"max_restarts": 3, "cooldown_s": 60}, "heartbeat": {"url": "${HC_URL}"}},
            storage={"dir": "x", "min_free_mb": 500, "max_media_gb": 20, "backups": {"interval_h": 12, "keep": 3}},
            logging={"max_mb": 5, "backups": 3, "level": "warning"},
        )
        config["cameras"][0]["source"]["faults"] = [{"type": "freeze", "at_s": 5, "duration_s": 10}]
        validate_config(config)

    def test_invalid_settings_are_rejected(self):
        config = self.config(
            health={"frame_timeout_s": 0, "supervisor": {"max_restarts": 0}, "heartbeat": {"url": "ftp://x"}},
            storage={"min_free_mb": -1, "backups": {"keep": 0}},
            logging={"level": "chatty"},
        )
        config["cameras"][0]["source"]["faults"] = [{"type": "meteor", "at_s": 1}]
        with self.assertRaises(ValueError) as caught:
            validate_config(config)
        message = str(caught.exception)
        for fragment in ("health.frame_timeout_s", "health.supervisor.max_restarts", "health.heartbeat.url", "storage.min_free_mb",
                         "storage.backups.keep", "logging.level", "faults[0].type"):
            self.assertIn(fragment, message)

    def test_faults_only_on_simulated_cameras(self):
        config = self.config()
        config["cameras"][0]["source"] = {"type": "rtsp", "host": "10.0.0.5", "faults": [{"type": "freeze", "at_s": 1}]}
        with self.assertRaises(ValueError) as caught:
            validate_config(config)
        self.assertIn("only allowed on simulated cameras", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
