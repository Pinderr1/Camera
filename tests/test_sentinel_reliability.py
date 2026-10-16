"""Failure scenarios against a real SentinelService with simulated cameras:
disconnects, frozen video, inference failures (GPU fallback and total AI
loss), crashed and stalled workers, whole-network outages, disk-full and
database-write failures, service restarts, and database/config recovery.

Health timings are shortened (1 s timeouts, sub-second backoff) so each
scenario runs in seconds; the logic is the same as with production defaults."""

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from sentinel.backup import create_backup
from sentinel.models import Incident, SecurityEvent
from sentinel.simulation import reset_fault_clocks

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

FULL = [[0, 0], [1, 0], [1, 1], [0, 1]]
Usage = namedtuple("Usage", "total used free")


def wait_for(predicate, timeout=20.0, interval=0.1, message="condition not reached"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(message)


def site_config(directory, cameras=("cam_a",), faults=None, health=None, storage=None, **extra):
    scenario = Path(directory) / "empty_scenario.json"
    scenario.write_text(json.dumps({"duration_s": 60, "actors": []}))
    faults = faults or {}
    return {
        "site": {"id": "reliability_site", "name": "Reliability test"},
        "cameras": [
            {
                "id": camera_id, "name": camera_id.replace("_", " ").title(), "fps": 5,
                "source": {"type": "simulated", "scenario": str(scenario)} | ({"faults": faults[camera_id]} if camera_id in faults else {}),
                "zones": [{"id": f"{camera_id}_yard", "name": "Yard", "type": "yard", "polygon": FULL}],
            }
            for camera_id in cameras
        ],
        "rules": [{"id": "yard_entry", "type": "zone_entry", "severity": "high", "zone_types": ["yard"]}],
        "detector": {"type": "scripted", "retry_s": 0.5},
        "environment": {"enabled": False},
        "health": {
            "frame_timeout_s": 1, "frozen_after_s": 1, "startup_grace_s": 10, "notify_after_s": 1,
            "supervisor": {"backoff_initial_s": 0.2, "backoff_max_s": 1, "stall_after_s": 1.5, "max_restarts": 5, "window_s": 60, "cooldown_s": 600},
        } | (health or {}),
        "storage": {"dir": str(Path(directory) / "store"), "check_interval_s": 1} | (storage or {}),
        "drone": {"enabled": False},
    } | extra


class ServiceCase(unittest.TestCase):
    def setUp(self):
        reset_fault_clocks()
        self.directory = tempfile.TemporaryDirectory()
        self.services = []
        self.notifications = []

    def tearDown(self):
        for service in self.services:
            try:
                service.stop()
            except Exception:
                pass
        time.sleep(0.2)
        self.directory.cleanup()

    def service(self, config, *, start=True, **kwargs):
        from sentinel.service import SentinelService

        service = SentinelService(copy.deepcopy(config), **kwargs)
        service.runtime.bus.subscribe(lambda topic, payload: self.notifications.append(payload) if topic == "notification" else None)
        self.services.append(service)
        if start:
            service.start()
        return service

    def state(self, service, camera_id="cam_a"):
        return service.runtime.camera_status[camera_id].get("health", {}).get("state")

    def titles(self):
        return [notification["title"] for notification in self.notifications]


class CameraFailureTests(ServiceCase):
    def test_camera_disconnect_is_reported_and_reconnect_restores_monitoring(self):
        service = self.service(site_config(self.directory.name, faults={"cam_a": [{"type": "disconnect", "at_s": 2, "duration_s": 4}]}))
        wait_for(lambda: self.state(service) == "monitoring", message="camera never started monitoring")
        wait_for(lambda: self.state(service) == "offline", message="disconnect not detected")
        status = service.runtime.camera_status["cam_a"]
        self.assertFalse(status["health"]["monitoring"])
        self.assertEqual(status["health"]["reason"], "simulated disconnect")
        wait_for(lambda: "Camera not monitoring: Cam A" in self.titles(), message="outage not notified")
        wait_for(lambda: self.state(service) == "monitoring", message="did not recover after reconnect")
        wait_for(lambda: "Monitoring restored: Cam A" in self.titles(), message="recovery not notified")
        outages = service.runtime.store.list_outages()
        self.assertEqual([(o["camera_id"], o["kind"], o["notified"]) for o in outages], [("cam_a", "offline", True)])
        self.assertIsNotNone(outages[0]["ended_ms"])
        types = [event["event_type"] for event in service.runtime.store.list_events()]
        self.assertIn("camera_offline", types)
        self.assertIn("camera_online", types)
        self.assertEqual(service.runtime.store.list_incidents(), [], "coverage problems never open security incidents")
        self.assertGreaterEqual(service.runtime.camera_status["cam_a"]["reconnects"], 1)

    def test_frozen_video_is_not_reported_as_monitoring(self):
        service = self.service(site_config(self.directory.name, faults={"cam_a": [{"type": "freeze", "at_s": 2, "duration_s": 4}]}))
        wait_for(lambda: self.state(service) == "monitoring")
        wait_for(lambda: self.state(service) == "frozen", message="frozen feed not detected")
        self.assertTrue(service.runtime.camera_status["cam_a"]["connected"], "frames keep arriving while frozen")
        wait_for(lambda: self.state(service) == "monitoring", timeout=15)
        self.assertEqual(service.runtime.store.list_outages()[0]["kind"], "frozen")

    def test_still_clean_scene_is_unverified_never_frozen(self):
        config = site_config(self.directory.name)
        config["cameras"][0]["source"]["noise"] = 0  # a perfectly clean, unchanging picture apart from the clock
        service = self.service(config)
        wait_for(lambda: self.state(service) == "unverified", message="still scene not reported as unverified")
        time.sleep(2)
        health = service.runtime.camera_status["cam_a"]["health"]
        self.assertEqual(health["state"], "unverified", "a still scene must never become frozen without evidence")
        self.assertTrue(health["monitoring"] and not health["verified"])
        self.assertIn("cannot be told apart", health["reason"])
        coverage = service.runtime.health.coverage(service.runtime.camera_status)
        self.assertEqual((coverage["state"], coverage["unverified"]), ("unverified", 1))
        self.assertFalse([t for t in self.titles() if "not monitoring" in t], "an uncertain still picture is not a coverage alarm")
        outage = service.runtime.store.list_outages()[0]
        self.assertEqual((outage["kind"], outage["details"].get("uncertain")), ("unverified", True))
        signals = health["signals"]
        self.assertTrue(signals["frames"]["ok"] and signals["inference"]["ok"] and signals["stream_clock"]["ok"])
        self.assertFalse(signals["content"]["ok"])

    def test_noisy_static_scene_stays_monitoring(self):
        service = self.service(site_config(self.directory.name))  # simulated sensor noise, nothing moving
        wait_for(lambda: self.state(service) == "monitoring")
        time.sleep(3)  # three times frozen_after_s
        self.assertEqual(self.state(service), "monitoring")
        self.assertEqual(service.runtime.camera_status["cam_a"]["content"]["static_since_ms"], 0)

    def test_frozen_picture_under_a_ticking_recorder_clock_is_detected(self):
        service = self.service(site_config(self.directory.name, faults={"cam_a": [{"type": "freeze_overlay", "at_s": 3, "duration_s": 5}]}))
        wait_for(lambda: self.state(service) == "monitoring")
        wait_for(lambda: self.state(service) == "frozen", message="frozen picture behind a live clock not detected")
        status = service.runtime.camera_status["cam_a"]
        self.assertIn("clock", status["health"]["reason"])
        self.assertTrue(status["health"]["signals"]["stream_clock"]["ok"], "the stream clock keeps advancing; the picture is the evidence")
        wait_for(lambda: self.state(service) == "monitoring", timeout=15)

    def test_gpu_inference_failure_falls_back_to_cpu(self):
        class FakeDetector:
            def __init__(self, provider):
                self.providers = [provider]

            def detect(self, frame):
                if self.providers[0] != "CPUExecutionProvider":
                    raise RuntimeError("DirectML device was removed")
                return []

            def close(self):
                pass

        def create(settings, threshold):
            return FakeDetector("CPUExecutionProvider" if settings.get("providers") == ["cpu"] else "DmlExecutionProvider")

        with mock.patch("sentinel.pipeline.create_detector", create):
            service = self.service(site_config(self.directory.name))
            status = service.runtime.camera_status["cam_a"]
            wait_for(lambda: status.get("detector_state") == "fallback", message="no CPU fallback")
            wait_for(lambda: self.state(service) == "monitoring")
            self.assertEqual(status["detector_provider"], "cpu")
            self.assertEqual(status["detector_errors"], 3)
            self.assertIn("DirectML", status["detector_error"])
            wait_for(lambda: "cpu_fallback" in status["health"]["warnings"])
            self.assertEqual(service.runtime.store.list_outages(), [], "a working CPU fallback keeps the camera monitored")

    def test_failing_animal_detector_never_costs_camera_coverage(self):
        from sentinel.detectors import AnimalAugmentedDetector

        class Primary:
            providers = ["CPUExecutionProvider"]

            def detect(self, frame):
                return []

            def close(self):
                pass

        class BrokenAnimalModel(Primary):
            def detect(self, frame):
                raise RuntimeError("MegaDetector inference failed")

        with mock.patch("sentinel.pipeline.create_detector", lambda settings, threshold: AnimalAugmentedDetector(Primary(), BrokenAnimalModel())), \
                self.assertLogs("sentinel.detectors", "WARNING"):
            service = self.service(site_config(self.directory.name))
            wait_for(lambda: self.state(service) == "monitoring")
            components = service.runtime.health.components
            wait_for(lambda: "animal_detector:cam_a" in components, message="animal detector failure not reported")
        self.assertEqual(components["animal_detector:cam_a"]["state"], "degraded")
        self.assertIn("MegaDetector inference failed", components["animal_detector:cam_a"]["detail"])
        status = service.runtime.camera_status["cam_a"]
        self.assertEqual((status["detector_errors"], status.get("detector_state")), (0, "ok"), "the main detector is unaffected")
        self.assertEqual(self.state(service), "monitoring")
        self.assertFalse([n for n in self.notifications if "nimal detector" in n["title"]], "an optional model is not pushed")

    def test_total_ai_failure_reports_not_monitoring_and_recovers(self):
        service = self.service(site_config(self.directory.name, faults={"cam_a": [{"type": "detector_error", "at_s": 2, "duration_s": 6}]}))
        wait_for(lambda: self.state(service) == "monitoring")
        wait_for(lambda: self.state(service) == "ai_failed", message="AI failure not reported")
        status = service.runtime.camera_status["cam_a"]
        self.assertEqual(status["health"]["reason"], "RuntimeError: simulated inference failure")
        self.assertTrue(status["connected"], "video still arrives, but nothing analyses it")
        wait_for(lambda: self.state(service) == "monitoring", timeout=20, message="detector never reloaded")
        self.assertGreaterEqual(status["detector_errors"], 3)
        self.assertEqual(service.runtime.store.list_outages()[0]["kind"], "ai_failed")

    def test_crashed_worker_is_restarted_with_backoff(self):
        service = self.service(site_config(self.directory.name, faults={"cam_a": [{"type": "crash", "at_s": 2, "duration_s": 0.3}]}))
        wait_for(lambda: self.state(service) == "monitoring")
        wait_for(lambda: service.runtime.camera_status["cam_a"].get("restarts_total", 0) >= 1, message="crash not restarted")
        wait_for(lambda: self.state(service) == "monitoring", message="no recovery after restart")
        outage = service.runtime.store.list_outages()[0]
        self.assertIn("restarting", outage["states"])
        self.assertIn("simulated capture crash", outage["reason"])
        audit = [row["action"] for row in service.runtime.store.list_audit()]
        self.assertIn("camera_worker_restarted", audit)

    def test_crash_loop_stops_at_the_restart_budget(self):
        config = site_config(self.directory.name, faults={"cam_a": [{"type": "crash", "at_s": 1}]},
                             health={"supervisor": {"backoff_initial_s": 0.1, "backoff_max_s": 0.2, "max_restarts": 2, "window_s": 60, "cooldown_s": 600}})
        service = self.service(config)
        wait_for(lambda: self.state(service) == "failed", message="restart loop was not stopped")
        restarts = service.runtime.camera_status["cam_a"]["restarts_total"]
        time.sleep(3)
        self.assertEqual(service.runtime.camera_status["cam_a"]["restarts_total"], restarts, "no further restarts while the circuit is open")
        self.assertEqual(restarts, 3)
        wait_for(lambda: any(n["severity"] == "high" and "Cam A" in n["title"] for n in self.notifications), message="failure not notified")
        self.assertGreater(service.runtime.camera_status["cam_a"]["next_restart_ms"], time.time() * 1000 + 500_000)

    def test_stalled_worker_is_abandoned_and_replaced(self):
        config = site_config(self.directory.name, faults={"cam_a": [{"type": "stall", "at_s": 2, "duration_s": 5}]},
                             health={"supervisor": {"stall_after_s": 1, "backoff_initial_s": 0.2, "max_stuck_threads": 10}})
        service = self.service(config)
        wait_for(lambda: self.state(service) == "monitoring")
        wait_for(lambda: "stalled" in service.runtime.camera_status["cam_a"].get("worker_error", ""), message="stall not detected")
        wait_for(lambda: self.state(service) == "monitoring" and not service.supervisor.abandoned, timeout=20,
                 message="replacement did not take over, or the stuck thread never ended")

    def test_too_many_stuck_threads_requests_a_process_restart(self):
        config = site_config(self.directory.name, faults={"cam_a": [{"type": "stall", "at_s": 1, "duration_s": 30}]},
                             health={"supervisor": {"stall_after_s": 0.5, "backoff_initial_s": 0.1, "backoff_max_s": 0.1, "max_stuck_threads": 1}})
        service = self.service(config, start=False)
        fatal = []
        service.on_fatal = fatal.append
        service.start()
        wait_for(lambda: fatal, timeout=20, message="fatal condition not raised")
        self.assertEqual(service.runtime.health.components["process"]["state"], "failed")

    def test_network_outage_on_every_camera_is_one_grouped_notification(self):
        outage = [{"type": "disconnect", "at_s": 2, "duration_s": 4}]
        service = self.service(site_config(self.directory.name, cameras=("cam_a", "cam_b"), faults={"cam_a": outage, "cam_b": outage}))
        wait_for(lambda: self.state(service, "cam_a") == self.state(service, "cam_b") == "monitoring")
        wait_for(lambda: service.runtime.health.coverage(service.runtime.camera_status)["state"] == "none", message="coverage loss not seen")
        wait_for(lambda: "2 cameras not monitoring" in self.titles())
        self.assertEqual(sum(1 for title in self.titles() if "not monitoring" in title), 1)
        wait_for(lambda: service.runtime.health.coverage(service.runtime.camera_status)["state"] == "full", timeout=20)


class StorageFailureTests(ServiceCase):
    def test_disk_full_pauses_recording_but_not_detection_and_resumes(self):
        free = {"mb": 50_000}
        usage = lambda path: Usage(100_000 * 2**20, 0, free["mb"] * 2**20)  # noqa: E731
        service = self.service(site_config(self.directory.name, storage={"min_free_mb": 1000, "min_free_pct": 1}), disk_usage=usage)
        wait_for(lambda: self.state(service) == "monitoring" and "cam_a" in service.runtime.recorders)
        free["mb"] = 200
        wait_for(lambda: service.runtime.recorders["cam_a"].paused, message="recording not paused")
        self.assertEqual(service.runtime.health.components["storage"]["state"], "failed")
        wait_for(lambda: "Recording storage needs attention" in self.titles())
        self.assertEqual(self.state(service), "monitoring", "detection continues on a full disk")
        self.assertEqual(service.runtime.health.coverage(service.runtime.camera_status)["state"], "degraded")
        free["mb"] = 50_000
        wait_for(lambda: not service.runtime.recorders["cam_a"].paused, message="recording not resumed")
        wait_for(lambda: service.runtime.health.components["storage"]["state"] == "ok")

    def test_recording_write_failure_keeps_monitoring(self):
        with mock.patch("sentinel.recorder.ClipRecorder.add_frame", side_effect=OSError(28, "No space left on device")):
            service = self.service(site_config(self.directory.name))
            wait_for(lambda: service.runtime.health.components.get("storage", {}).get("state") == "degraded")
            time.sleep(1.5)
            self.assertEqual(self.state(service), "monitoring")
            self.assertIn("No space left", service.runtime.health.components["storage"]["detail"])

    def test_database_write_failure_still_sends_alerts(self):
        service = self.service(site_config(self.directory.name), start_cameras=False)
        runtime = service.runtime
        runtime.update_settings({"arm_mode": "away"}, "test")
        real = runtime.store._db

        class FullDisk:
            def __getattr__(self, name):
                return getattr(real, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                    raise sqlite3.OperationalError("database or disk is full")
                return real.execute(sql, *args)

        runtime.store._db = FullDisk()
        event = SecurityEvent("evt_full", "zone_entry", "high", "cam_a", int(time.time() * 1000), int(time.time() * 1000), 0.9,
                              "Person entered Yard", zone_id="cam_a_yard", site_track_id="st_9")
        runtime._handle_events([event], event.timestamp_ms)
        self.assertTrue(any(n.get("incident_id") and n["severity"] == "high" for n in self.notifications), "the alert still goes out")
        self.assertFalse(runtime.store.last_write_ok)
        wait_for(lambda: runtime.health.components.get("database", {}).get("state") == "degraded")
        wait_for(lambda: "Event database needs attention" in self.titles())
        runtime.store._db = real
        runtime.store.audit("test", "write_probe", {})
        wait_for(lambda: runtime.health.components["database"]["state"] == "ok")

    def test_protected_incident_media_survives_retention_and_space_purges(self):
        service = self.service(site_config(self.directory.name), start_cameras=False)
        store, media = service.runtime.store, service.runtime.media_root
        old = time.time() - 30 * 86400
        for name in ("keep.mp4", "drop.mp4"):
            path = media / "clips" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            import os

            os.utime(path, (old, old))
        now_ms = int(time.time() * 1000)
        month_ago = now_ms - 30 * 86_400_000
        store.upsert_incident(Incident("inc_keep", month_ago, month_ago, "high", "Person entered Yard", status="closed", clip_paths=["clips/keep.mp4"], protected=True))
        store.upsert_incident(Incident("inc_drop", month_ago, month_ago, "high", "Person entered Yard", status="closed", clip_paths=["clips/drop.mp4"]))
        store.purge(events_before_ms=now_ms - 7 * 86_400_000, media_before_ms=now_ms - 7 * 86_400_000, media_root=media,
                    protected_before_ms=now_ms - 90 * 86_400_000)
        self.assertTrue((media / "clips" / "keep.mp4").exists())
        self.assertFalse((media / "clips" / "drop.mp4").exists())
        self.assertIsNotNone(store.get_incident("inc_keep"))
        self.assertIsNone(store.get_incident("inc_drop"))
        store.purge(events_before_ms=now_ms, media_before_ms=now_ms, media_root=media, protected_before_ms=now_ms)
        self.assertFalse((media / "clips" / "keep.mp4").exists(), "protection is long, not forever")


class RestartAndRecoveryTests(ServiceCase):
    def test_clean_restart_is_recorded_without_alarm_and_a_crash_is_notified(self):
        config = site_config(self.directory.name)
        first = self.service(config, start_cameras=False)
        first.stop()
        self.services.remove(first)
        second = self.service(config, start_cameras=False)
        outages = second.runtime.store.list_outages()
        self.assertEqual([(o["scope"], o["kind"], o["notified"]) for o in outages], [("service", "service_stopped", False)])
        self.assertEqual(self.notifications, [], "a planned stop is logged, not pushed")
        second.stop()
        self.services.remove(second)
        state_path = Path(config["storage"]["dir"]) / "runstate.json"
        now = int(time.time() * 1000)
        state_path.write_text(json.dumps({"pid": 4_000_000, "started_ms": now - 600_000, "heartbeat_ms": now - 300_000, "clean": False}))
        third = self.service(config, start_cameras=False)
        crash = next(o for o in third.runtime.store.list_outages() if o["kind"] != "service_stopped")
        self.assertEqual((crash["kind"], crash["notified"]), ("service_crash", True))
        self.assertGreaterEqual(crash["duration_s"], 299)
        self.assertTrue(self.notifications[-1]["title"].startswith("Sentinel was not monitoring for 5 min"))

    def test_second_instance_on_the_same_storage_is_refused(self):
        config = site_config(self.directory.name)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            Path(config["storage"]["dir"]).mkdir(parents=True, exist_ok=True)
            (Path(config["storage"]["dir"]) / "runstate.json").write_text(json.dumps({"pid": child.pid, "heartbeat_ms": int(time.time() * 1000), "clean": False}))
            with self.assertRaisesRegex(RuntimeError, "already using"):
                self.service(config, start_cameras=False)
        finally:
            child.kill()
            child.wait()

    def test_corrupt_database_is_restored_from_the_latest_backup(self):
        config = site_config(self.directory.name)
        first = self.service(config, start_cameras=False)
        first.runtime.store.add_event(SecurityEvent("evt_before_backup", "activity", "info", "cam_a", 1000, 1000, 1.0, "Person in Yard"))
        first.create_backup(label="auto")
        first.runtime.store.add_event(SecurityEvent("evt_after_backup", "activity", "info", "cam_a", 2000, 2000, 1.0, "Person in Yard"))
        first.stop()
        self.services.remove(first)
        storage = Path(config["storage"]["dir"])
        (storage / "sentinel.db").write_bytes(b"this is not a database" * 100)
        second = self.service(config, start_cameras=False)
        self.assertEqual(second.db_recovery["action"], "restored_from_backup")
        ids = [event["event_id"] for event in second.runtime.store.list_events()]
        self.assertIn("evt_before_backup", ids)
        self.assertNotIn("evt_after_backup", ids)
        self.assertTrue(list((storage / "recovery").glob("sentinel.db.*.corrupt")), "the damaged file is kept for inspection")
        self.assertIn("Sentinel repaired its event database", self.titles())
        self.assertIn("database_recovered", [row["action"] for row in second.runtime.store.list_audit()])

    def test_corrupt_database_without_backup_starts_empty_and_says_so(self):
        config = site_config(self.directory.name)
        storage = Path(config["storage"]["dir"])
        storage.mkdir(parents=True)
        (storage / "sentinel.db").write_bytes(b"\x00garbage" * 512)
        service = self.service(config, start_cameras=False)
        self.assertEqual(service.db_recovery["action"], "started_empty")
        service.runtime.store.add_event(SecurityEvent("evt_new", "activity", "info", "cam_a", 1, 1, 1.0, "x"))
        self.assertEqual(len(service.runtime.store.list_events()), 2, "the recovery notice itself plus the new event")

    def test_recovery_preserves_the_damaged_original_and_reports_backup_age_and_loss_window(self):
        import hashlib

        config = site_config(self.directory.name)
        first = self.service(config, start_cameras=False)
        first.create_backup(label="auto")
        first.stop()
        self.services.remove(first)
        storage = Path(config["storage"]["dir"])
        damaged = b"SQLite format 3\x00" + b"\xde\xad" * 4000
        (storage / "sentinel.db").write_bytes(damaged)
        for suffix in ("-wal", "-shm"):
            Path(str(storage / "sentinel.db") + suffix).unlink(missing_ok=True)
        second = self.service(config, start_cameras=False)
        report = second.db_recovery
        preserved = storage / "recovery" / report["quarantined"][0]["file"]
        self.assertEqual(preserved.read_bytes(), damaged, "the damaged original is kept byte for byte")
        self.assertEqual(report["quarantined"][0]["sha256"], hashlib.sha256(damaged).hexdigest())
        self.assertGreaterEqual(report["backup_age_s"], 0)
        self.assertLessEqual(report["possible_loss"]["from_ms"], report["possible_loss"]["to_ms"])
        self.assertGreaterEqual(report["restored_ms"], report["detected_ms"])
        saved = json.loads((storage / "recovery" / report["report_file"]).read_text())
        self.assertEqual(saved["backup"], report["backup"])
        message = next(n["message"] for n in self.notifications if n["title"] == "Sentinel repaired its event database")
        self.assertIn("may be missing", message)
        self.assertIn("old)", message)
        self.assertIn("preserved unchanged", message)

    def test_readable_rows_are_salvaged_from_a_copy_of_the_damaged_database(self):
        from sentinel.store import EventStore

        config = site_config(self.directory.name)
        first = self.service(config, start_cameras=False)
        first.runtime.store.add_event(SecurityEvent("evt_backed_up", "activity", "info", "cam_a", 1000, 1000, 1.0, "Person in Yard"))
        first.create_backup(label="auto")
        first.runtime.store.add_event(SecurityEvent("evt_after_backup", "zone_entry", "high", "cam_a", 5000, 5000, 0.9, "Person entered Yard"))
        first.stop()
        self.services.remove(first)
        checks = iter(["*** in database main ***\nPage 7: btree page corrupted"])
        real = EventStore.quick_check
        with mock.patch.object(EventStore, "quick_check", lambda store: next(checks, None) or real(store)):
            second = self.service(config, start_cameras=False)
        report = second.db_recovery
        self.assertEqual(report["action"], "restored_from_backup")
        self.assertGreaterEqual(report["merged"].get("events", 0), 1)
        ids = [event["event_id"] for event in second.runtime.store.list_events(limit=1000)]
        self.assertIn("evt_backed_up", ids)
        self.assertIn("evt_after_backup", ids, "rows written after the backup were recovered from the damaged copy")
        message = next(n["message"] for n in self.notifications if n["title"] == "Sentinel repaired its event database")
        self.assertIn("Recovered from the damaged file", message)

    def test_offline_and_staged_restores_never_overwrite_the_current_database(self):
        from sentinel.backup import restore_backup, stage_database_restore

        config = site_config(self.directory.name)
        first = self.service(config, start_cameras=False)
        name = first.create_backup(label="manual")["name"]
        first.runtime.store.add_event(SecurityEvent("evt_current", "activity", "info", "cam_a", 1, 1, 1.0, "x"))
        first.stop()
        self.services.remove(first)
        storage = Path(config["storage"]["dir"])
        current = (storage / "sentinel.db").read_bytes()
        result = restore_backup(storage / "backups" / name, storage, parts=("database",))
        self.assertEqual((storage / "recovery" / result["preserved"][0]["file"]).read_bytes(), current)
        stage_database_restore(storage / "backups" / name, storage)
        second = self.service(config, start_cameras=False)
        self.assertEqual(second.db_recovery["action"], "staged_restore")
        self.assertTrue(second.db_recovery["quarantined"], "the database replaced by a staged restore is kept")
        self.assertIn("Event history restored from a backup", self.titles())

    def test_damaged_site_config_falls_back_to_the_last_good_version(self):
        config = site_config(self.directory.name)
        first = self.service(config, start_cameras=False)
        first.site.set_categories({"animal": {"detect": True, "alerts": True}}, "tester")
        first.site.set_categories({"animal": {"detect": True, "alerts": False}}, "tester")
        first.stop()
        self.services.remove(first)
        storage = Path(config["storage"]["dir"])
        (storage / "site.json").write_text('{"site": {"id": "broken"')  # torn write
        second = self.service(config, start_cameras=False)
        self.assertEqual(second.site.config["categories"]["animal"]["alerts"], True, "the previous saved version is used")
        self.assertTrue(list((storage / "recovery").glob("site.*.damaged.json")))
        self.assertIn("Sentinel repaired its configuration", self.titles())


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class SystemApiTests(ServiceCase):
    TOKEN = "reliability-token"

    def setUp(self):
        super().setUp()
        from sentinel.api import create_app

        config = site_config(self.directory.name, cameras=("cam_a",))
        config["cameras"].append({"id": "cam_rtsp", "name": "Porch", "enabled": False,
                                  "source": {"type": "rtsp", "host": "10.0.0.20", "path": "/secret-path", "username": "admin", "credential": "porch"}})
        self.config = config
        self.svc = self.service(config)
        self.client = TestClient(create_app(self.svc, api_token=self.TOKEN))
        self.auth = {"Authorization": f"Bearer {self.TOKEN}", "X-Sentinel-Operator": "tester"}

    def test_health_report_requires_auth_and_never_contains_stream_addresses(self):
        self.assertEqual(self.client.get("/api/v1/system/health").status_code, 401)
        wait_for(lambda: self.client.get("/api/v1/system/health", headers=self.auth).json()["coverage"]["state"] == "full")
        report = self.client.get("/api/v1/system/health", headers=self.auth).json()
        camera = report["cameras"][0]
        self.assertEqual((camera["camera_id"], camera["health"]["state"]), ("cam_a", "monitoring"))
        self.assertIn("latency_ms_p95", camera["metrics"])
        self.assertIn("free_mb", report["resources"]["disk"])
        text = json.dumps(report)
        for secret in ("/secret-path", "rtsp://", "credential"):
            self.assertNotIn(secret, text)
        state = self.client.get("/api/v1/state", headers=self.auth).json()
        self.assertEqual(state["coverage"]["state"], "full")
        snapshot = self.client.get("/api/v1/cameras/cam_a/snapshot.jpg", headers=self.auth)
        self.assertEqual(snapshot.headers["X-Sentinel-Monitoring"], "true")

    def test_restart_endpoint_and_outage_history(self):
        wait_for(lambda: self.state(self.svc) == "monitoring")
        before = self.svc.workers["cam_a"]
        self.assertEqual(self.client.post("/api/v1/cameras/cam_a/restart", headers=self.auth).json(), {"ok": True})
        self.assertIsNot(self.svc.workers["cam_a"], before)
        self.assertEqual(self.client.post("/api/v1/cameras/nope/restart", headers=self.auth).status_code, 404)
        wait_for(lambda: self.state(self.svc) == "monitoring")
        self.assertIsInstance(self.client.get("/api/v1/system/outages", headers=self.auth).json(), list)
        self.assertIn("camera_restarted", [row["action"] for row in self.client.get("/api/v1/audit", headers=self.auth).json()])

    def test_backup_create_verify_and_restore(self):
        self.client.patch("/api/v1/settings", headers=self.auth, json={"arm_mode": "away"})
        created = self.client.post("/api/v1/system/backups", headers=self.auth, json={"label": "manual"}).json()
        names = [item["name"] for item in self.client.get("/api/v1/system/backups", headers=self.auth).json()]
        self.assertIn(created["name"], names)
        self.assertIn("sentinel.db", created["files"])
        self.assertNotIn("credentials.json", created["files"], "passwords are only backed up on request")
        self.assertTrue(self.client.post(f"/api/v1/system/backups/{created['name']}/verify", headers=self.auth).json()["ok"])
        self.svc.site.set_categories({"animal": {"detect": True, "alerts": True}}, "tester")
        self.client.patch("/api/v1/settings", headers=self.auth, json={"arm_mode": "home"})
        restored = self.client.post(f"/api/v1/system/backups/{created['name']}/restore", headers=self.auth,
                                    json={"parts": ["config", "settings", "database"]}).json()
        self.assertEqual((restored["applied"], restored["staged"]), (["config", "settings"], ["database"]))
        self.assertNotIn("animal", self.svc.site.config.get("categories", {}))
        self.assertEqual(self.svc.runtime.settings["arm_mode"], "away")
        self.assertTrue((Path(self.config["storage"]["dir"]) / "restore_pending" / "sentinel.db").exists())
        for bad in ("..%2Fsentinel.db", "sentinel-x.txt", "nope.zip"):
            self.assertIn(self.client.post(f"/api/v1/system/backups/{bad}/verify", headers=self.auth).status_code, (404, 422))
        self.assertEqual(self.client.post("/api/v1/system/backups", headers=self.auth, json={"label": "../x"}).status_code, 422)

    def test_staged_database_restore_applies_at_next_start(self):
        runtime = self.svc.runtime
        runtime.store.add_event(SecurityEvent("evt_kept", "activity", "info", "cam_a", 1000, 1000, 1.0, "Person in Yard"))
        name = self.client.post("/api/v1/system/backups", headers=self.auth, json={}).json()["name"]
        runtime.store.add_event(SecurityEvent("evt_rolled_back", "activity", "info", "cam_a", 2000, 2000, 1.0, "Person in Yard"))
        self.client.post(f"/api/v1/system/backups/{name}/restore", headers=self.auth, json={"parts": ["database"]})
        self.svc.stop()
        self.services.remove(self.svc)
        reset_fault_clocks()
        restarted = self.service(self.config, start_cameras=False)
        ids = [event["event_id"] for event in restarted.runtime.store.list_events(limit=1000)]
        self.assertIn("evt_kept", ids)
        self.assertNotIn("evt_rolled_back", ids)
        self.assertEqual(restarted.db_recovery["action"], "staged_restore")

    def test_incident_protection_endpoint(self):
        event = SecurityEvent("evt_p", "zone_entry", "high", "cam_a", 1000, 1000, 0.9, "Person entered Yard", zone_id="cam_a_yard", site_track_id="st_p")
        self.svc.runtime._handle_events([event], 1000)
        incident_id = self.client.get("/api/v1/incidents", headers=self.auth).json()[0]["incident_id"]
        protected = self.client.post(f"/api/v1/incidents/{incident_id}/protect", headers=self.auth, json={"protected": True}).json()
        self.assertTrue(protected["protected"])
        self.assertEqual(self.client.post("/api/v1/incidents/inc_missing/protect", headers=self.auth, json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
