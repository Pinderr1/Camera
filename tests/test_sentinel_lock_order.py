"""Drone/FLAME readers must not wait on the editable site lock.

Site saves synchronously apply configuration while holding that lock, and
applying configuration needs the drone fleet lock held by the safety loop.
"""

import tempfile
import threading
import unittest

from sentinel.config import load_config
from sentinel.service import SentinelService


class DroneConfigLockOrderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = load_config("config/sentinel.farm.example.json", resolve_secrets=False)
        config["storage"] = {"dir": self.directory.name}
        self.service = SentinelService(config, start_cameras=False)

    def tearDown(self):
        self.service.stop()
        self.directory.cleanup()

    def assert_reader_completes_during_save(self, read):
        finished = threading.Event()
        errors = []

        def reader():
            try:
                # Match the safety loop's fleet lock, then manager readiness.
                with self.service._drone_lock:
                    read()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with self.service.site._lock:
            worker = threading.Thread(target=reader, daemon=True)
            worker.start()
            completed = finished.wait(2)
        # Release the site lock even on failure so the regression cannot hang.
        worker.join(5)
        self.assertFalse(worker.is_alive(), "reader failed to exit after releasing the site lock")
        self.assertFalse(errors, errors)
        self.assertTrue(completed, "drone/FLAME reader waited on the site save lock")

    def test_drone_readiness_does_not_wait_for_site_save(self):
        self.assert_reader_completes_during_save(self.service.drone.status)

    def test_flame_hooks_do_not_wait_for_site_save(self):
        self.assert_reader_completes_during_save(lambda: self.service.flame.step(0.1))

    def test_accessory_edits_reach_hooks_without_rebuilding_the_drone(self):
        manager = self.service.drone
        self.service.site.save_device("burn_kit", {"name": "Updated kit"}, "operator")
        self.assertIs(self.service.drone, manager)
        self.assertEqual(self.service.flame.accessory("burn_kit")["name"], "Updated kit")
        self.service.site.delete_device("burn_kit", "operator")
        self.service.flame.apply_hooks()
        self.assertIs(self.service.drone, manager)
        self.assertIsNone(self.service.flame.accessory_for_drone("farm_drone"))
        self.assertNotIn("accessory", {check["id"] for check in manager.readiness()["checks"]})

    def test_api_reads_during_site_edits_and_fleet_rebuilds(self):
        from fastapi.testclient import TestClient
        from sentinel.api import create_app

        self.service.start()
        stop = threading.Event()
        started = threading.Event()
        errors = []

        def read_api():
            try:
                with TestClient(create_app(self.service, api_token=None)) as client:
                    while not stop.is_set():
                        for path in ("/api/v1/drone", "/api/v1/drone/journey", "/api/v1/devices"):
                            response = client.get(path)
                            if response.status_code != 200:
                                raise AssertionError((path, response.status_code, response.text))
                        started.set()
                        stop.wait(0.01)
            except Exception as error:
                errors.append(error)
                started.set()

        reader = threading.Thread(target=read_api, daemon=True)
        reader.start()
        try:
            self.assertTrue(started.wait(5), "API reader did not start")
            for index in range(3):
                self.service.site.save_device("farm_drone", {"name": f"Farm drone {index}"}, "operator")
                self.service.site.save_device("burn_kit", {"name": f"Kit {index}"}, "operator")
        finally:
            stop.set()
            reader.join(10)
        self.assertFalse(reader.is_alive(), "API requests deadlocked during fleet rebuild")
        self.assertFalse(errors, errors)
