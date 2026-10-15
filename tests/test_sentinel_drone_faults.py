"""The drone fault-injection framework without a simulator: the faulty radio,
the faulty camera, the scenario runner's verdicts and the report. The SITL
scenarios are in scripts/sitl/fault_scenarios.py
(tests/test_sentinel_drone_faults_sitl.py runs them)."""

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from sentinel.drone.faults import FaultLink, FaultyVideoSource, Scenario, VideoFault, run_scenarios, summarise, write_report

try:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink.dialects.v20 import ardupilotmega as mavlink2
except ImportError:
    mavlink2 = None


def frame(message):
    mav = mavlink2.MAVLink(None, srcSystem=1, srcComponent=1)
    return message.pack(mav)


@unittest.skipIf(mavlink2 is None, "pymavlink not installed (used only to build test frames)")
class FaultLinkTests(unittest.TestCase):
    def setUp(self):
        self.socks = {}
        for name in ("fc", "hub", "pilot"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.5)
            self.socks[name] = sock
            self.addCleanup(sock.close)
        port = lambda name: self.socks[name].getsockname()[1]  # noqa: E731
        self.link = FaultLink(f"udp:127.0.0.1:{port('fc')}", f"udp:127.0.0.1:{port('hub')}", [f"udp:127.0.0.1:{port('pilot')}"]).start()
        self.addCleanup(self.link.stop)
        deadline = time.time() + 3
        while self.link._fc_sock is None and time.time() < deadline:
            time.sleep(0.02)
        # the flight controller needs to know the relay's address: the relay sends nothing first, so learn it from a Hub heartbeat
        self.socks["hub"].sendto(frame(mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3)), ("127.0.0.1", self.link._hub_sock.getsockname()[1]))
        _, self.relay_fc = self.socks["fc"].recvfrom(4096)

    def send(self, *messages):
        self.socks["fc"].sendto(b"".join(frame(m) for m in messages), self.relay_fc)

    def received(self, name, timeout=0.5):
        sock, frames = self.socks[name], []
        sock.settimeout(timeout)
        try:
            while True:
                data, _ = sock.recvfrom(65536)
                frames += [m.get_type() for m in mavlink2.MAVLink(None).parse_buffer(data) or []]
        except socket.timeout:
            return frames

    def test_filter_loss_duplicate_and_the_pilot_side_untouched(self):
        battery = mavlink2.MAVLink_sys_status_message(0, 0, 0, 0, 12000, 0, 80, 0, 0, 0, 0, 0, 0)
        heartbeat = mavlink2.MAVLink_heartbeat_message(2, 3, 0, 0, 4, 3)
        self.link.drop_ids = {1}
        self.send(heartbeat, battery)
        self.assertEqual(self.received("hub"), ["HEARTBEAT"], "battery reports filtered out for the Hub")
        self.assertEqual(sorted(self.received("pilot")), ["HEARTBEAT", "SYS_STATUS"], "the pilot still gets everything")
        self.link.reset_faults()
        self.link.duplicate = True
        self.send(heartbeat)
        self.assertEqual(self.received("hub"), ["HEARTBEAT", "HEARTBEAT"])
        self.link.reset_faults()
        self.link.loss = 1.0
        self.send(heartbeat, heartbeat)
        self.assertEqual(self.received("hub"), [])
        self.assertEqual(self.link.counts["lost"], 2)

    def test_delay_and_injection(self):
        self.link.delay_s = 0.6
        started = time.monotonic()
        self.send(mavlink2.MAVLink_heartbeat_message(2, 3, 0, 0, 4, 3))
        self.assertEqual(self.received("hub", timeout=0.3), [], "not yet")
        self.assertEqual(self.received("hub", timeout=1.0), ["HEARTBEAT"])
        self.assertGreaterEqual(time.monotonic() - started, 0.55)
        self.link.reset_faults()
        self.link.inject_to_hub(frame(mavlink2.MAVLink_heartbeat_message(2, 3, 128, 0, 4, 3)))
        self.assertEqual(self.received("hub"), ["HEARTBEAT"])
        self.link.set_hub_link(False)
        self.received("pilot", timeout=0.2)  # what the pilot got earlier
        self.send(mavlink2.MAVLink_heartbeat_message(2, 3, 0, 0, 4, 3))
        self.assertEqual(self.received("hub"), [])
        self.assertEqual(self.received("pilot"), ["HEARTBEAT"])


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FaultyVideoTests(unittest.TestCase):
    def test_schedule(self):
        from types import SimpleNamespace

        class Source:
            n = 0

            def read(self):
                self.n += 1
                return SimpleNamespace(image=SimpleNamespace(copy=lambda n=self.n: f"img{n}"), timestamp_ms=self.n, truth=None, meta={})

            def close(self):
                pass

        clock = FakeClock()
        source = FaultyVideoSource(Source(), [VideoFault("freeze", 1, 2), VideoFault("disconnect", 2, 3)], clock=clock)
        first = source.read()
        clock.now += 1.2
        frozen = source.read()
        self.assertIsNotNone(frozen)
        clock.now += 1.0
        self.assertIsNone(source.read(), "disconnected")
        clock.now += 1.0
        self.assertIsNotNone(source.read())
        self.assertEqual(source.applied, {"freeze": 1, "disconnect": 1})
        self.assertIsNotNone(first)


class RunnerTests(unittest.TestCase):
    def test_prepare_failure_stops_without_claiming_later_cases_were_tested(self):
        prepared = []

        def broken_prepare(scenario):
            prepared.append(scenario.id)
            raise RuntimeError("simulator unavailable")

        results = run_scenarios([self.scenario(lambda ctx: ctx.expect("ready", True))] * 2,
                                model="sentinel_lite", prepare=broken_prepare)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "error")
        self.assertIn("simulator unavailable", results[0].error)

    def test_restore_failure_stops_before_preparing_another_scenario(self):
        prepared = []

        def prepare(scenario):
            prepared.append(scenario.id)
            return self.prepare(scenario)

        def broken_restore(ctx):
            raise RuntimeError("signing lockout not restored")

        results = run_scenarios([self.scenario(lambda ctx: ctx.expect("ready", True))] * 2,
                                model="sentinel_lite", prepare=prepare, finish=broken_restore)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "error")
        self.assertIn("restore failed", results[0].error)

    def test_deadline_stops_a_scenario_that_would_wait_forever_and_still_restores(self):
        clock = FakeClock()
        restored = []

        def sleep(seconds):
            clock.now += seconds

        def endless(ctx):
            ctx.expect("ready", True)
            ctx.wait_until(lambda s: False, 10_000, "never")

        results = run_scenarios([self.scenario(endless)], model="sentinel_lite", prepare=self.prepare,
                                finish=lambda ctx: restored.append(ctx.scenario.id), clock=clock, sleep=sleep, deadline_s=60)
        self.assertEqual(results[0].status, "error")
        self.assertIn("deadline", results[0].error)
        self.assertLess(results[0].duration_s, 62)
        self.assertEqual(restored, ["S1"])

    def scenario(self, run, **extra):
        return Scenario("S1", "gps", "test", "on the ground", "nothing", "nothing", "nothing", run=run, **extra)

    def prepare(self, scenario):
        state = {"mode": "LOITER", "n": 0}

        def tick():
            state["n"] += 1
            if state["n"] == 3:
                state["mode"] = "RTL"

        return {"state": state}, lambda: {"mode": state["mode"]}, tick

    def test_verdicts(self):
        clock = FakeClock()

        def sleep(seconds):
            clock.now += seconds

        def passing(ctx):
            ctx.expect("returns home", ctx.wait_until(lambda s: s["mode"] == "RTL", 10, "RTL"), "mode RTL")

        def failing(ctx):
            ctx.expect("never lands", ctx.wait_until(lambda s: s["mode"] == "LAND", 2, "LAND"))

        def silent(ctx):
            ctx.run_for(1)

        def crashing(ctx):
            raise RuntimeError("simulator vanished")

        restored = []
        scenarios = [self.scenario(passing), self.scenario(failing), self.scenario(silent), self.scenario(crashing),
                     self.scenario(None, limitation="SITL has no camera hardware"), self.scenario(passing, models=("sentinel_pro",))]
        results = run_scenarios(scenarios, model="sentinel_lite", prepare=self.prepare, finish=lambda ctx: restored.append(ctx.scenario.id),
                                clock=clock, sleep=sleep)
        self.assertEqual([r.status for r in results], ["pass", "fail", "error", "error", "not_modelled", "not_modelled"])
        self.assertIn("made no checks", results[2].error)
        self.assertIn("simulator vanished", results[3].error)
        self.assertEqual(len(restored), 4, "the simulator is restored after every scenario that ran")
        self.assertIn({"t": 1.0, "field": "mode", "value": "RTL"}, results[0].timeline)
        self.assertEqual(summarise(results), {"pass": 1, "fail": 1, "error": 2, "not_modelled": 2})
        with tempfile.TemporaryDirectory() as directory:
            paths = write_report(results, directory, environment={"firmware": "ArduCopter 4.7.2 SITL"})
            text = Path(paths["markdown"]).read_text(encoding="utf-8")
            self.assertIn("| NOT MODELLED | 2 |", text)
            self.assertIn("SITL has no camera hardware", text)
            self.assertEqual(json.loads(Path(paths["json"]).read_text())["summary"]["pass"], 1)


if __name__ == "__main__":
    unittest.main()
