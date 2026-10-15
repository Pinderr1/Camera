"""Fault-injection framework for drone reliability scenarios.

A scenario states, before it runs, the initial conditions, the injected
failure, what the aircraft (ArduPilot) is expected to do and what Sentinel is
expected to say and do. The runner executes it, records a timeline of state
transitions from real telemetry, evaluates explicit checks, and writes a
report. A scenario passes only when every check passed: finishing without an
exception proves nothing. A scenario that cannot be modelled faithfully says
so (`not_modelled`) instead of reporting a result.

Building blocks (standard library only):
- `FaultLink`: a MAVLink link between the flight controller and the Hub and the
  pilot's ground station that can lose, delay, duplicate or filter the Hub's
  traffic, cut either side, or inject frames (a second autopilot). It stands
  in for the radio, like the companion relay's pass-through mode.
- `FaultyVideoSource`: wraps a drone video source and freezes, blanks, drops
  or slows it on a schedule.
- `Scenario`, `ScenarioContext`, `run_scenarios`, `write_report`.

The SITL scenarios themselves live in scripts/sitl/fault_scenarios.py (they
need the simulator-only ground-station stand-in); tests/test_sentinel_drone_faults.py
checks the framework without a simulator."""

from __future__ import annotations

import heapq
import json
import random
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..companion.relay import FrameParser, MavlinkRelay

# ---- the faulty radio ------------------------------------------------------------------


class FaultLink(MavlinkRelay):
    """Pass-through relay (policy `sitl`, simulators only) with fault knobs on
    the flight controller -> Hub direction. The pilot's side is untouched
    unless `set_gcs_link(False)`, so the flight controller's own failsafes see
    exactly what a real radio fault would give them."""

    def __init__(self, fc: str, hub: str, gcs: list[str] | None = None, *, seed: int = 1) -> None:
        super().__init__(fc, hub, gcs, policy="sitl")
        self.loss = 0.0  # probability a frame to the Hub is lost
        self.delay_s = 0.0
        self.duplicate = False
        self.drop_ids: set[int] = set()  # message ids never delivered to the Hub
        self.counts = {"to_hub": 0, "lost": 0, "filtered": 0, "duplicated": 0, "injected": 0}
        self._rng = random.Random(seed)
        self._fc_parser = FrameParser()
        self._queue: list[tuple[float, int, bytes]] = []
        self._queue_lock = threading.Condition()
        self._seq = 0
        self._last_due = 0.0
        self._sender = threading.Thread(target=self._send_delayed, name="faultlink-delay", daemon=True)
        self._sender.start()

    def reset_faults(self) -> None:
        self.loss, self.delay_s, self.duplicate, self.drop_ids = 0.0, 0.0, False, set()
        self.set_hub_link(True)
        self.set_gcs_link(True)

    def inject_to_hub(self, raw: bytes) -> None:
        """Deliver frames to the Hub as if the flight controller's radio carried them."""
        self.counts["injected"] += 1
        self._deliver(raw)

    def _from_fc(self, sock: Any) -> None:
        scheme = self.fc_endpoint.split(":")[0]
        if scheme == "tcp":
            data = sock.recv(65536)
            if not data:
                raise OSError("closed by the flight controller")
        else:
            data, peer = sock.recvfrom(65536)
            if scheme == "udpin":
                self._fc_peer = peer
        self.stats.fc_frames += 1
        self.stats.last_fc_ms = int(time.time() * 1000)
        if self.gcs_link_up:
            for gcs_sock, address in self._gcs_socks:
                gcs_sock.sendto(data, address)
        frames = self._fc_parser.feed(data)
        if not self.hub_link_up:
            return
        for frame in frames:
            if frame.msgid in self.drop_ids:
                self.counts["filtered"] += 1
                continue
            if self.loss and self._rng.random() < self.loss:
                self.counts["lost"] += 1
                continue
            self._deliver(frame.raw)
            if self.duplicate:
                self.counts["duplicated"] += 1
                self._deliver(frame.raw)

    def _deliver(self, raw: bytes) -> None:
        """First in, first out, like a real radio: when a delay ends, frames still
        queued go out before newer ones (a reordering link is a different fault)."""
        self.counts["to_hub"] += 1
        with self._queue_lock:
            if self.delay_s <= 0 and not self._queue:
                self._hub_sock.sendto(raw, self.hub_address)
                return
            due = max(time.monotonic() + self.delay_s, self._last_due)
            self._last_due = due
            self._seq += 1
            heapq.heappush(self._queue, (due, self._seq, raw))
            self._queue_lock.notify()

    def _send_delayed(self) -> None:
        while not self._stop.is_set():
            with self._queue_lock:
                if not self._queue:
                    self._queue_lock.wait(0.2)
                    continue
                due, _, raw = self._queue[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._queue_lock.wait(min(wait, 0.2))
                    continue
                heapq.heappop(self._queue)
                try:  # sent under the lock, so a frame delivered directly cannot overtake it
                    self._hub_sock.sendto(raw, self.hub_address)
                except OSError:
                    pass


# ---- the faulty camera ------------------------------------------------------------------


@dataclass
class VideoFault:
    kind: str  # freeze | blank | disconnect | slow
    start_s: float
    end_s: float
    factor: float = 4.0  # slow: deliver one frame in `factor`


class FaultyVideoSource:
    """Wraps a video source; faults are scheduled in seconds since the first read."""

    def __init__(self, source: Any, faults: list[VideoFault], clock: Callable[[], float] = time.monotonic) -> None:
        self.source = source
        self.faults = faults
        self.clock = clock
        self.started: float | None = None
        self.held = None
        self.delivered = 0
        self.applied: dict[str, int] = {}

    def _active(self) -> VideoFault | None:
        elapsed = self.clock() - (self.started or self.clock())
        return next((f for f in self.faults if f.start_s <= elapsed < f.end_s), None)

    def read(self) -> Any:
        if self.started is None:
            self.started = self.clock()
        fault = self._active()
        if fault and fault.kind == "disconnect":
            self.source.read()  # keep the underlying source moving
            self.applied["disconnect"] = self.applied.get("disconnect", 0) + 1
            time.sleep(0.05)
            return None
        frame = self.source.read()
        if frame is None:
            return None
        if fault is None:
            self.held = frame
            return frame
        self.applied[fault.kind] = self.applied.get(fault.kind, 0) + 1
        if fault.kind == "freeze":
            if self.held is None:
                self.held = frame
            return type(frame)(image=self.held.image.copy(), timestamp_ms=frame.timestamp_ms, truth=self.held.truth, meta=dict(frame.meta))
        if fault.kind == "blank":
            image = frame.image.copy()
            image[:] = 0
            return type(frame)(image=image, timestamp_ms=frame.timestamp_ms, truth=[], meta=dict(frame.meta))
        if fault.kind == "slow":
            self.delivered += 1
            return frame if self.delivered % max(1, int(fault.factor)) == 0 else None
        return frame

    def close(self) -> None:
        self.source.close()

    def status(self) -> dict[str, Any]:
        return getattr(self.source, "status", lambda: {})() | {"faults_applied": dict(self.applied)}


# ---- scenarios -----------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    evidence: str


@dataclass
class Scenario:
    id: str
    group: str  # gps | geofence | battery | comms | video | operations | security
    title: str
    initial: str
    fault: str
    expected_aircraft: str
    expected_sentinel: str
    run: Callable[["ScenarioContext"], None] | None = None
    models: tuple[str, ...] = ("sentinel_lite",)
    limitation: str = ""  # what SITL cannot model; a scenario without `run` is reported as not modelled


@dataclass
class ScenarioResult:
    id: str
    group: str
    title: str
    model: str
    status: str  # pass | fail | error | not_modelled
    initial: str
    fault: str
    expected_aircraft: str
    expected_sentinel: str
    checks: list[Check] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)  # facts measured during the run (modes, times, values)
    recovery: str = ""
    limitation: str = ""
    error: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScenarioTimeout(Exception):
    """A scenario ran past its deadline: reported as an error, never as a result."""


class ScenarioContext:
    """What a scenario's `run` works with. `world` holds the scenario-specific
    objects (manager, ground station, link, health monitor...), set up by the
    caller's `prepare`; `snapshot` returns the fields the timeline follows."""

    def __init__(self, scenario: Scenario, model: str, world: dict[str, Any], snapshot: Callable[[], dict[str, Any]],
                 tick: Callable[[], None], clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 deadline_s: float | None = None) -> None:
        self.scenario = scenario
        self.model = model
        self.world = world
        self._snapshot = snapshot
        self._tick = tick
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.deadline = self.started + deadline_s if deadline_s else None
        self.checks: list[Check] = []
        self.timeline: list[dict[str, Any]] = []
        self.observed: dict[str, Any] = {}
        self.recovery = ""
        self._last: dict[str, Any] = {}
        self._cleanups: list[Callable[[], None]] = []

    # ---- recording ----------------------------------------------------------------

    def t(self) -> float:
        return round(self.clock() - self.started, 1)

    def note(self, event: str, **data: Any) -> None:
        self.timeline.append({"t": self.t(), "event": event} | data)

    def sample(self) -> dict[str, Any]:
        """Advance Sentinel one tick and record every field that changed.
        Every wait goes through here, so this is where the deadline holds."""
        if self.deadline is not None and self.clock() > self.deadline:
            self.note("timed out: scenario deadline")
            raise ScenarioTimeout(f"scenario exceeded its {self.deadline - self.started:g} s deadline")
        self._tick()
        state = self._snapshot()
        for key, value in state.items():
            if self._last.get(key, object()) != value:
                self.timeline.append({"t": self.t(), "field": key, "value": value})
        self._last = state
        return state

    def wait_until(self, predicate: Callable[[dict[str, Any]], bool], timeout_s: float, what: str, interval_s: float = 0.5) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            state = self.sample()
            if predicate(state):
                self.note(f"reached: {what}")
                return True
            self.sleep(interval_s)
        self.note(f"timed out: {what}")
        return False

    def run_for(self, seconds: float, interval_s: float = 0.5) -> dict[str, Any]:
        deadline = self.clock() + seconds
        state = self.sample()
        while self.clock() < deadline:
            self.sleep(interval_s)
            state = self.sample()
        return state

    def seen(self, field_name: str, value: Any = None, predicate: Callable[[Any], bool] | None = None) -> bool:
        """Did the timeline ever show this field with this value (or matching)?"""
        for entry in self.timeline:
            if entry.get("field") != field_name:
                continue
            if predicate is not None and predicate(entry["value"]):
                return True
            if predicate is None and entry["value"] == value:
                return True
        return False

    def first_time(self, field_name: str, predicate: Callable[[Any], bool]) -> float | None:
        return next((entry["t"] for entry in self.timeline if entry.get("field") == field_name and predicate(entry["value"])), None)

    # ---- checks -------------------------------------------------------------------

    def expect(self, name: str, passed: bool, evidence: str = "") -> bool:
        self.checks.append(Check(name, bool(passed), evidence))
        self.note(("PASS: " if passed else "FAIL: ") + name)
        return bool(passed)

    def cleanup(self, call: Callable[[], None]) -> None:
        self._cleanups.append(call)

    def close(self) -> None:
        for call in reversed(self._cleanups):
            try:
                call()
            except Exception as error:  # recovery problems are reported, never hidden
                self.note("cleanup failed", error=f"{type(error).__name__}: {error}")


def run_scenarios(scenarios: list[Scenario], *, model: str, prepare: Callable[[Scenario], tuple[dict[str, Any], Callable[[], dict[str, Any]], Callable[[], None]]],
                  finish: Callable[[ScenarioContext], None] | None = None, on_result: Callable[[ScenarioResult], None] | None = None,
                  clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                  deadline_s: float | None = None) -> list[ScenarioResult]:
    """Run each scenario: `prepare` builds its world and returns (world, snapshot, tick);
    `finish` restores the simulator (always called). `deadline_s` bounds each
    scenario's run (restoration has its own bounded waits)."""
    results = []
    for scenario in scenarios:
        result = ScenarioResult(scenario.id, scenario.group, scenario.title, model, "not_modelled", scenario.initial, scenario.fault,
                                scenario.expected_aircraft, scenario.expected_sentinel, limitation=scenario.limitation)
        if scenario.run is None or model not in scenario.models:
            if model not in scenario.models:
                result.limitation = result.limitation or f"not applicable to {model}"
            results.append(result)
            if on_result:
                on_result(result)
            continue
        started = clock()
        context = None
        restore_failed = False
        try:
            world, snapshot, tick = prepare(scenario)
            context = ScenarioContext(scenario, model, world, snapshot, tick, clock=clock, sleep=sleep, deadline_s=deadline_s)
            scenario.run(context)
            if not context.checks:
                result.status, result.error = "error", "the scenario made no checks: finishing is not a result"
            else:
                result.status = "pass" if all(check.passed for check in context.checks) else "fail"
        except Exception as error:
            result.status, result.error = "error", f"{type(error).__name__}: {error}\n{traceback.format_exc(limit=6)}"
        finally:
            if context is not None:
                context.close()
                if finish:
                    try:
                        finish(context)
                    except Exception as error:
                        result.error += f"\nrestore failed: {type(error).__name__}: {error}"
                        result.status = "error"
                        restore_failed = True
                result.checks, result.timeline, result.observed, result.recovery = context.checks, context.timeline, context.observed, context.recovery
        result.duration_s = round(clock() - started, 1)
        results.append(result)
        if on_result:
            on_result(result)
        if restore_failed or context is None:
            # Continuing would test later scenarios against a poisoned world.
            # Keep completed evidence; omitted scenarios have not been tested.
            break
    return results


# ---- report -------------------------------------------------------------------------------

STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "not_modelled": "NOT MODELLED"}


def summarise(results: list[ScenarioResult]) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_LABEL}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def write_report(results: list[ScenarioResult], out_dir: str | Path, *, title: str = "Sentinel drone reliability scenarios",
                 environment: dict[str, Any] | None = None) -> dict[str, str]:
    """results.json (everything, including timelines) and report.md (for people)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    counts = summarise(results)
    data = {"title": title, "environment": environment or {}, "summary": counts, "results": [result.as_dict() for result in results]}
    (out / "results.json").write_text(json.dumps(data, indent=2, default=str))
    lines = [f"# {title}", ""]
    for key, value in (environment or {}).items():
        lines.append(f"- **{key}**: {value}")
    lines += ["", "| Result | Count |", "| --- | --- |"] + [f"| {STATUS_LABEL[k]} | {v} |" for k, v in counts.items()] + [""]
    lines += ["| Scenario | Group | Model | Result | Time (s) |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {r.id}: {r.title} | {r.group} | {r.model} | {STATUS_LABEL[r.status]} | {r.duration_s:g} |" for r in results]
    for r in results:
        lines += ["", f"## {r.id}: {r.title} ({STATUS_LABEL[r.status]})", "",
                  f"- **Initial conditions:** {r.initial}", f"- **Injected failure:** {r.fault}",
                  f"- **Expected aircraft behaviour:** {r.expected_aircraft}", f"- **Expected Sentinel behaviour:** {r.expected_sentinel}"]
        if r.limitation:
            lines.append(f"- **Limitation:** {r.limitation}")
        if r.observed:
            lines.append("- **Observed:** " + "; ".join(f"{k} = {v}" for k, v in r.observed.items()))
        if r.recovery:
            lines.append(f"- **Recovery:** {r.recovery}")
        if r.checks:
            lines += ["", "| Check | Result | Evidence |", "| --- | --- | --- |"]
            lines += [f"| {c.name} | {'pass' if c.passed else 'FAIL'} | {c.evidence.replace('|', '/')[:300]} |" for c in r.checks]
        if r.error:
            lines += ["", "```text", r.error.strip()[:2000], "```"]
        transitions = [e for e in r.timeline if "field" in e][:60]
        if transitions:
            lines += ["", "<details><summary>State transitions (first 60)</summary>", "", "```text"]
            lines += [f"{e['t']:>7.1f}s  {e['field']}: {e['value']}" for e in transitions]
            lines += ["```", "</details>"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(out / "results.json"), "markdown": str(out / "report.md")}


__all__ = ["Check", "FaultLink", "FaultyVideoSource", "Scenario", "ScenarioContext", "ScenarioResult", "VideoFault", "run_scenarios",
           "summarise", "write_report"]
