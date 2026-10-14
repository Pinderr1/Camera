"""Process-level reliability: the `sentinel run` supervisor restarts a crashed
server with backoff and a restart budget, refuses to loop on configuration
errors, and a stop request shuts everything down gracefully. The end-to-end
test kills the real serving process and checks that monitoring comes back and
that the gap is recorded as an outage."""

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from sentinel.daemon import EXIT_CONFIG, RunState, request_stop, supervise

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=60.0, interval=0.2, message="condition not reached"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = predicate()
        except Exception:
            value = None
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(message)


class SupervisorProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []

    def tearDown(self):
        self.directory.cleanup()

    def run_supervisor(self, code, stop_after=None, **settings):
        if stop_after is not None:
            threading.Timer(stop_after, request_stop, args=(self.directory.name,)).start()
        command = [sys.executable, "-c", f"import sys; sys.exit({code})"]
        return supervise(command, self.directory.name, on_event=lambda kind, data: self.events.append((kind, data)), poll_s=0.05, **settings)

    def test_crashing_child_is_restarted_with_backoff_then_the_budget_slows_it_down(self):
        result = self.run_supervisor(1, stop_after=4.0, backoff_initial_s=0.1, backoff_max_s=1, max_restarts=3, window_s=60, cooldown_s=30)
        self.assertEqual(result, 0, "a stop request during the cooldown ends supervision cleanly")
        delays = [data["restart_in_s"] for kind, data in self.events if kind == "crashed"]
        self.assertEqual(delays, [0.1, 0.2, 0.4, 30], "exponential backoff, then the cooldown instead of a tight loop")
        self.assertEqual(sum(1 for kind, _ in self.events if kind == "started"), 4)

    def test_configuration_errors_are_not_retried(self):
        self.assertEqual(self.run_supervisor(EXIT_CONFIG), EXIT_CONFIG)
        self.assertEqual([kind for kind, _ in self.events], ["started", "config_error"])

    def test_clean_exit_ends_supervision(self):
        self.assertEqual(self.run_supervisor(0), 0)
        self.assertEqual([kind for kind, _ in self.events], ["started", "exited"])


class ServiceRunEndToEndTests(unittest.TestCase):
    def test_killed_server_is_restarted_and_stops_gracefully(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "store"
            scenario = Path(directory) / "scenario.json"
            scenario.write_text(json.dumps({"duration_s": 60, "actors": []}))
            port = free_port()
            config = {
                "site": {"id": "process_test", "name": "Process test"},
                "cameras": [{"id": "cam_a", "name": "Cam A", "source": {"type": "simulated", "scenario": str(scenario)},
                             "zones": [{"id": "yard", "name": "Yard", "type": "yard", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}],
                "rules": [],
                "detector": {"type": "scripted"},
                "environment": {"enabled": False},
                "health": {"process": {"backoff_initial_s": 0.5, "backoff_max_s": 2}},
                "storage": {"dir": str(storage), "backups": {"enabled": False}},
                "api": {"host": "127.0.0.1", "port": port},
                "drone": {"enabled": False},
            }
            config_path = Path(directory) / "site.json"
            config_path.write_text(json.dumps(config))
            env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
            env.pop("SENTINEL_API_TOKEN", None)
            runner = subprocess.Popen([sys.executable, "-m", "sentinel", "run", "--config", str(config_path)], cwd=REPO, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                health = lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2).status == 200  # noqa: E731
                wait_for(health, message="server never came up")
                first = wait_for(lambda: RunState(storage).read())
                self.assertNotEqual(first["pid"], runner.pid, "the server runs as a child of the supervisor")
                os.kill(first["pid"], signal.SIGTERM)  # on Windows: TerminateProcess, i.e. an abrupt crash
                second = wait_for(lambda: (state := RunState(storage).read()) and state["pid"] != first["pid"] and state, message="server not restarted")
                wait_for(health, message="server did not come back after the crash")
                request_stop(storage)
                self.assertEqual(runner.wait(timeout=90), 0)
                final = RunState(storage).read()
                self.assertEqual((final["pid"], final["clean"]), (second["pid"], True), "the restarted server stopped cleanly")
                database = sqlite3.connect(storage / "sentinel.db")
                kinds = [row[0] for row in database.execute("SELECT kind FROM outages WHERE scope = 'service'")]
                database.close()
                self.assertIn("service_crash", kinds, "the gap caused by the crash is in the outage history")
                self.assertTrue((storage / "logs" / "sentinel.log").exists())
                self.assertTrue((storage / "logs" / "supervisor.log").exists())
                self.assertFalse((storage / "control" / "stop").exists())
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait()
                state = RunState(storage).read() or {}
                if state.get("pid") and not state.get("clean"):
                    try:
                        os.kill(state["pid"], signal.SIGTERM)
                    except OSError:
                        pass
                time.sleep(0.5)


if __name__ == "__main__":
    unittest.main()
