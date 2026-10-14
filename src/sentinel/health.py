"""Coverage health: is each camera actually being monitored right now?

The monitor turns raw worker and source signals (heartbeats, last processed
frame, frozen-image detection, detector failures, supervisor restarts) into one
honest state per camera, records outages, and sends debounced notifications
when security coverage is degraded. A camera is only ever reported as
`monitoring` while frames are arriving, changing, and passing through the
detector. Standard library only, like the rest of the core."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .incidents import DISCLAIMER
from .models import Notification

# Display text for each camera state. Every state except `monitoring` and
# `unverified` means the camera is NOT watching the property, and the dashboard and
# API say so. `unverified` keeps analysing frames, but Sentinel cannot prove the
# picture is live (a still, noise-free scene looks exactly like a frozen feed).
CAMERA_STATES = {
    "monitoring": "Monitoring",
    "unverified": "Live picture cannot be confirmed",
    "starting": "Starting",
    "offline": "No video",
    "frozen": "Video frozen",
    "ai_failed": "AI processing failed",
    "stalled": "Processing stalled",
    "restarting": "Processing crashed; restarting",
    "failed": "Processing stopped after repeated crashes",
    "not_running": "Not running (cameras disabled in this session)",
}
# Event type written to the timeline when a camera enters each state.
STATE_EVENTS = {
    "unverified": "camera_unverified",
    "offline": "camera_offline",
    "frozen": "camera_frozen",
    "ai_failed": "camera_ai_failed",
    "stalled": "camera_stalled",
    "restarting": "camera_restarting",
    "failed": "camera_failed",
}
HEALTH_EVENT_TYPES = set(STATE_EVENTS.values()) | {"camera_online", "system_health"}
COMPONENT_LABELS = {
    "storage": "Recording storage",
    "database": "Event database",
    "service": "Sentinel service",
    "notifications": "Notification delivery",
    "process": "Sentinel process",
}


@dataclass
class HealthSettings:
    frame_timeout_s: float = 10.0  # no processed frame for this long: the camera is not monitoring
    frozen_after_s: float = 60.0  # frames arrive but the picture has not changed at all
    startup_grace_s: float = 30.0  # time allowed for a (re)started camera to deliver its first frame
    notify_after_s: float = 120.0  # an outage this long is pushed to people
    notifications: bool = True
    notify_recovery: bool = True
    max_notifications_per_hour: int = 6
    slow_ratio: float = 0.5  # processing below this share of the target fps ...
    slow_after_s: float = 60.0  # ... for this long is a performance warning
    inference_timeout_s: float = 15.0  # frames arrive but no inference completed for this long: AI not working
    unverified_notify_after_s: float = 21600.0  # an unprovable still picture is pushed (low) after this long; 0 = never
    supervisor: dict[str, Any] = field(default_factory=dict)
    heartbeat: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "HealthSettings":
        data = dict(data or {})
        if "notify_after_s" not in data and "camera_offline_after_s" in data:  # pre-0.9 key
            data["notify_after_s"] = data["camera_offline_after_s"]
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class Outage:
    outage_id: str
    scope: str  # camera | storage | database | service | notifications | process
    kind: str  # first state of the outage (offline, frozen, ...) or component condition
    started_ms: int
    camera_id: str = ""
    ended_ms: int | None = None
    reason: str = ""
    states: list[str] = field(default_factory=list)
    notified: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        end = self.ended_ms
        return {
            "outage_id": self.outage_id, "scope": self.scope, "kind": self.kind, "camera_id": self.camera_id,
            "started_ms": self.started_ms, "ended_ms": end, "reason": self.reason, "states": list(self.states),
            "notified": self.notified, "details": dict(self.details),
            "duration_s": round(((end or int(time.time() * 1000)) - self.started_ms) / 1000, 1),
        }


def camera_state(status: dict[str, Any], now_ms: int, settings: HealthSettings) -> tuple[str, str]:
    """(state, reason) for one camera from its runtime status fields.

    The signals are judged independently: frame delivery (`last_frame_ms`), the
    stream's own timestamps (`pts_stalled_since_ms`), picture content (`content`
    from `vision.ContentMonitor`), and the inference heartbeat
    (`last_inference_ms`). Frozen needs positive evidence: repeated stream
    timestamps, or a picture that lost its sensor noise abruptly. A still scene
    without that evidence is `unverified`, never `frozen`."""
    worker_state = status.get("worker_state")
    if worker_state is None:
        return "not_running", ""
    if worker_state == "failed":
        return "failed", status.get("worker_error", "")
    if worker_state == "restarting":
        return "restarting", status.get("worker_error", "")
    if status.get("stalled"):
        return "stalled", status.get("worker_error", "") or "the camera's processing thread stopped responding"
    if status.get("detector_state") == "failed":
        return "ai_failed", status.get("detector_error", "")
    started = int(status.get("worker_started_ms") or 0)
    last = int(status.get("last_frame_ms") or 0)
    if last < started:  # nothing processed since this worker started
        if now_ms - started < settings.startup_grace_s * 1000:
            return "starting", ""
        return "offline", status.get("error") or "no video received since start"
    if now_ms - last > settings.frame_timeout_s * 1000:
        return "offline", status.get("error") or "no new video"
    last_inference = int(status.get("last_inference_ms") or 0) or started
    if now_ms - last_inference > settings.inference_timeout_s * 1000:
        return "ai_failed", f"frames arrive but no detection has completed for {round((now_ms - last_inference) / 1000)} s"
    frozen_after_ms = settings.frozen_after_s * 1000
    pts_stalled = int(status.get("pts_stalled_since_ms") or 0)
    if pts_stalled and last - pts_stalled >= frozen_after_ms:
        return "frozen", "the stream keeps repeating the same frame (its timestamps stopped advancing)"
    content = status.get("content") or {}
    static_since = int(content.get("static_since_ms") or 0)
    if static_since and last - static_since >= frozen_after_ms:
        overlay = content.get("static_kind") == "overlay"
        if content.get("noisy_before_static"):
            return "frozen", "the picture stopped changing abruptly" + (" (only an on-screen overlay such as a clock still changes)" if overlay else "")
        return "unverified", ("the picture has not changed" + (" apart from a small overlay such as a clock" if overlay else "")
                              + "; a still scene cannot be told apart from a frozen feed")
    return "monitoring", ""


def camera_signals(status: dict[str, Any], now_ms: int, settings: HealthSettings) -> dict[str, Any]:
    """Each liveness signal on its own, for the System tab and the API."""
    last = int(status.get("last_frame_ms") or 0)
    content = status.get("content") or {}
    inference = int(status.get("last_inference_ms") or 0)
    pts_stalled = int(status.get("pts_stalled_since_ms") or 0)
    static_since = int(content.get("static_since_ms") or 0)
    return {
        "frames": {"ok": bool(last) and now_ms - last <= settings.frame_timeout_s * 1000, "last_ms": last or None, "fps": status.get("fps")},
        "decoder": {"ok": None if status.get("decoded_fps") is None else status["decoded_fps"] > 0,
                    "decoded_fps": status.get("decoded_fps"), "frames_decoded": status.get("frames_decoded")},
        "stream_clock": {"ok": None if status.get("pts_ms") is None else not pts_stalled, "pts_ms": status.get("pts_ms"),
                         "stalled_since_ms": pts_stalled or None},
        "content": {"ok": not static_since, "static_since_ms": static_since or None, "static_kind": content.get("static_kind") or None,
                    "noise_level": content.get("noise_level"), "last_change_ms": content.get("last_change_ms") or None},
        "inference": {"ok": bool(inference) and now_ms - inference <= settings.inference_timeout_s * 1000, "last_ms": inference or None,
                      "provider": status.get("detector_provider"), "state": status.get("detector_state")},
    }


class HealthMonitor:
    """Evaluated once a second by the runtime's housekeeping. Keeps the per-camera
    `health` block in camera status, opens and closes outages, writes timeline
    events, and asks the runtime to send notifications."""

    def __init__(
        self,
        settings: HealthSettings,
        *,
        save_outage: Callable[[Outage], None],
        record_event: Callable[[str, str, str, str, int], None],
        notify: Callable[[Notification], None],
        camera_name: Callable[[str], str],
    ) -> None:
        self.settings = settings
        self.save_outage = save_outage
        self.record_event = record_event  # (camera_id, event_type, severity, summary, now_ms)
        self.notify = notify
        self.camera_name = camera_name
        self.open: dict[str, Outage] = {}  # key: camera id or "component:<name>"
        self.components: dict[str, dict[str, Any]] = {}
        self._sent: deque[int] = deque()
        self._held = 0
        self._seq = 0
        self._slow_since: dict[str, int] = {}

    # ---- cameras ---------------------------------------------------------------

    def evaluate(self, camera_status: dict[str, dict[str, Any]], now_ms: int) -> None:
        for camera_id, status in camera_status.items():
            state, reason = camera_state(status, now_ms, self.settings)
            previous = status.get("health", {})
            since = previous.get("since_ms", now_ms) if previous.get("state") == state else now_ms
            warnings = self._warnings(camera_id, status, state, now_ms)
            status["health"] = {
                "state": state, "label": CAMERA_STATES[state], "monitoring": state in ("monitoring", "unverified"),
                "verified": state == "monitoring", "since_ms": since, "reason": reason, "warnings": warnings,
                "signals": camera_signals(status, now_ms, self.settings) if state != "not_running" else {},
            }
            status["offline_reported"] = camera_id in self.open  # kept for older readers (bench)
            self._track_camera_outage(camera_id, state, reason, since, now_ms)
        for camera_id in [key for key in self.open if not key.startswith("component:") and key not in camera_status]:
            self._close(camera_id, now_ms, "camera removed")
        self._send_due(now_ms)

    def _warnings(self, camera_id: str, status: dict[str, Any], state: str, now_ms: int) -> list[str]:
        warnings = []
        if status.get("detector_state") == "fallback":
            warnings.append("cpu_fallback")
        target = float(status.get("target_fps") or 0)
        if state == "monitoring" and target and float(status.get("fps") or 0) < target * self.settings.slow_ratio:
            self._slow_since.setdefault(camera_id, now_ms)
            if now_ms - self._slow_since[camera_id] >= self.settings.slow_after_s * 1000:
                warnings.append("slow_processing")
        else:
            self._slow_since.pop(camera_id, None)
        if int(status.get("restarts_recent") or 0):
            warnings.append("recent_restarts")
        return warnings

    def _track_camera_outage(self, camera_id: str, state: str, reason: str, since: int, now_ms: int) -> None:
        outage = self.open.get(camera_id)
        if state in ("monitoring", "starting", "not_running"):
            if outage is not None and state == "monitoring":
                self._close(camera_id, now_ms)
            return
        if outage is None:
            outage = self._new_outage("camera", state, since, camera_id=camera_id, reason=reason)
            if state == "unverified":  # still analysing: an uncertain period, pushed only after a long time
                outage.details["notify_after_s"] = self.settings.unverified_notify_after_s
                outage.details["uncertain"] = True
                self.save_outage(outage)
            self.open[camera_id] = outage
            self.record_event(camera_id, STATE_EVENTS[state], "info" if state == "unverified" else "medium",
                              f"{self.camera_name(camera_id)}: {CAMERA_STATES[state].lower()}" + (f" ({reason})" if reason else ""), now_ms)
        elif state not in outage.states:
            outage.states.append(state)
            outage.reason = reason or outage.reason
            if state != "unverified" and outage.details.pop("uncertain", None):  # the period became a real coverage loss
                outage.details.pop("notify_after_s", None)
                outage.notified = False  # a "cannot confirm" message does not cover a confirmed loss
            self.save_outage(outage)
            self.record_event(camera_id, STATE_EVENTS[state], "medium", f"{self.camera_name(camera_id)}: {CAMERA_STATES[state].lower()}"
                              + (f" ({reason})" if reason else ""), now_ms)
            if state == "failed" and outage.notified and self.settings.notifications:
                # Already reported as "restarting"; automatic recovery has now stopped, which needs a person.
                name = self.camera_name(camera_id)
                self.announce(f"Camera processing stopped: {name}",
                              [f"{name} kept crashing and automatic restarts are paused" + (f" ({reason})." if reason else "."),
                               "Use Restart on the System tab after fixing the cause; Sentinel also retries on its own later."],
                              "high", now_ms, outage.outage_id, (camera_id,))

    # ---- system components -----------------------------------------------------

    def set_component(self, name: str, state: str, detail: str, now_ms: int, *, notify: bool = True) -> None:
        """`state` is ok | degraded | failed. Non-ok states open an outage that is
        notified (when `notify`) after the same delay as camera outages."""
        current = self.components.get(name, {})
        if current.get("state") == state and current.get("detail") == detail:
            return
        since = current.get("since_ms", now_ms) if current.get("state") == state else now_ms
        self.components[name] = {"state": state, "detail": detail, "since_ms": since, "label": COMPONENT_LABELS.get(name, name)}
        key = f"component:{name}"
        if state == "ok":
            if key in self.open:
                self._close(key, now_ms)
                self.record_event("", "system_health", "info", f"{COMPONENT_LABELS.get(name, name)} back to normal", now_ms)
            return
        if key not in self.open:
            outage = self._new_outage(name, state, now_ms, reason=detail)
            outage.details["notify"] = notify
            self.open[key] = outage
            self.record_event("", "system_health", "medium", f"{COMPONENT_LABELS.get(name, name)}: {detail}", now_ms)
        else:
            self.open[key].reason = detail
            self.save_outage(self.open[key])

    def record_past_outage(self, scope: str, kind: str, started_ms: int, ended_ms: int, reason: str, *, notify: bool) -> Outage:
        """An outage discovered after the fact (Sentinel itself was not running)."""
        outage = self._new_outage(scope, kind, started_ms, reason=reason)
        outage.ended_ms = ended_ms
        if notify and self.settings.notifications:
            outage.notified = self.announce(
                f"Sentinel was not monitoring for {_duration(ended_ms - started_ms)}",
                [f"{reason}.", f"No camera was watched from {_clock(started_ms)} to {_clock(ended_ms)}.", "Monitoring has resumed."],
                "medium", ended_ms, outage.outage_id, (),
            )
        self.save_outage(outage)
        return outage

    # ---- summary -----------------------------------------------------------------

    def coverage(self, camera_status: dict[str, dict[str, Any]]) -> dict[str, Any]:
        states = {camera_id: status.get("health", {}).get("state", "starting") for camera_id, status in camera_status.items()}
        total = len(states)
        unverified = sum(1 for state in states.values() if state == "unverified")
        monitoring = sum(1 for state in states.values() if state == "monitoring") + unverified
        if not total:
            overall = "no_cameras"  # a drone- or sensor-first site: nothing to monitor continuously, nothing failing
        elif all(state == "not_running" for state in states.values()):
            overall = "not_running"
        elif total and monitoring == total:
            overall = "unverified" if unverified else "full"
        elif monitoring == 0:
            overall = "none"
        else:
            overall = "degraded"
        if overall in ("full", "unverified", "no_cameras") and any(c["state"] != "ok" for c in self.components.values()):
            overall = "degraded"
        return {
            "state": overall, "monitoring": monitoring, "unverified": unverified, "total": total,
            "not_monitoring": [
                {"camera_id": camera_id, "name": self.camera_name(camera_id), "state": state, "label": CAMERA_STATES.get(state, state)}
                for camera_id, state in states.items() if state not in ("monitoring", "unverified")
            ],
            "unverified_cameras": [
                {"camera_id": camera_id, "name": self.camera_name(camera_id), "state": state, "label": CAMERA_STATES[state]}
                for camera_id, state in states.items() if state == "unverified"
            ],
            "components": {name: dict(value) for name, value in self.components.items()},
        }

    # ---- internals -------------------------------------------------------------

    def _new_outage(self, scope: str, kind: str, started_ms: int, *, camera_id: str = "", reason: str = "") -> Outage:
        self._seq += 1
        outage = Outage(f"out_{started_ms}_{self._seq}", scope, kind, started_ms, camera_id=camera_id, reason=reason, states=[kind])
        self.save_outage(outage)
        return outage

    def _close(self, key: str, now_ms: int, note: str = "") -> None:
        outage = self.open.pop(key)
        outage.ended_ms = now_ms
        if note:
            outage.details["closed_because"] = note
        self.save_outage(outage)
        if outage.scope == "camera":
            name = self.camera_name(outage.camera_id)
            text = "picture is changing again" if outage.details.get("uncertain") else "monitoring again"
            self.record_event(outage.camera_id, "camera_online", "info", f"{name}: {text} after {_duration(now_ms - outage.started_ms)}", now_ms)
            if outage.notified and self.settings.notify_recovery and not note:
                self.announce(f"Monitoring restored: {name}", [f"{name} is being monitored again after {_duration(now_ms - outage.started_ms)}."],
                           "info", now_ms, outage.outage_id, (outage.camera_id,))
        elif outage.notified and self.settings.notify_recovery:
            label = COMPONENT_LABELS.get(outage.scope, outage.scope)
            self.announce(f"Back to normal: {label}", [f"{label} recovered after {_duration(now_ms - outage.started_ms)}."],
                       "info", now_ms, outage.outage_id, ())

    def close_all(self, now_ms: int, note: str) -> None:
        """Service shutdown: end every open outage (the restart records the gap)."""
        for key in list(self.open):
            outage = self.open.pop(key)
            outage.ended_ms = now_ms
            outage.details["closed_because"] = note
            self.save_outage(outage)

    def _send_due(self, now_ms: int) -> None:
        if not self.settings.notifications:
            return
        due = [
            outage for outage in self.open.values()
            if not outage.notified and outage.details.get("notify", True)
            and outage.details.get("notify_after_s", self.settings.notify_after_s) > 0
            and now_ms - outage.started_ms >= outage.details.get("notify_after_s", self.settings.notify_after_s) * 1000
        ]
        uncertain = [outage for outage in due if outage.details.get("uncertain")]
        for outage in uncertain:  # low priority, one camera per message: "please look at it"
            name = self.camera_name(outage.camera_id)
            if self.announce(f"Cannot confirm {name} is live", [
                f"{name} has shown an unchanging picture since {_clock(outage.started_ms)}. It may be a still scene or a frozen feed.",
                "Please check the camera view; Sentinel keeps analysing it.",
            ], "low", now_ms, outage.outage_id, (outage.camera_id,)):
                outage.notified = True
                self.save_outage(outage)
        due = [outage for outage in due if not outage.details.get("uncertain")]
        if not due:
            return
        cameras = [outage for outage in due if outage.scope == "camera"]
        components = [outage for outage in due if outage.scope != "camera"]
        lines = []
        for outage in cameras:
            state = outage.states[-1]
            lines.append(f"{self.camera_name(outage.camera_id)}: {CAMERA_STATES[state].lower()} since {_clock(outage.started_ms)}"
                         + (f" ({outage.reason})" if outage.reason else ""))
        for outage in components:
            lines.append(f"{COMPONENT_LABELS.get(outage.scope, outage.scope)}: {outage.reason}")
        if len(cameras) == 1 and not components:
            title = f"Camera not monitoring: {self.camera_name(cameras[0].camera_id)}"
        elif cameras:
            title = f"{len(cameras)} cameras not monitoring" if not components else "Security coverage degraded"
        else:
            title = f"{COMPONENT_LABELS.get(components[0].scope, components[0].scope)} needs attention" if len(components) == 1 else "Sentinel needs attention"
        severity = "high" if any("failed" in outage.states for outage in cameras) else "medium"
        if self.announce(title, lines + ["Sentinel cannot detect activity where coverage is lost."], severity, now_ms,
                      due[0].outage_id, tuple(outage.camera_id for outage in cameras)):
            for outage in due:
                outage.notified = True
                self.save_outage(outage)

    def announce(self, title: str, lines: list[str], severity: str, now_ms: int, reference: str, camera_ids: tuple[str, ...]) -> bool:
        window_ms = 3_600_000
        while self._sent and now_ms - self._sent[0] > window_ms:
            self._sent.popleft()
        if len(self._sent) >= self.settings.max_notifications_per_hour:
            self._held += 1
            return False
        self._sent.append(now_ms)
        if self._held:
            lines = lines + [f"{self._held} other health update(s) were held back; see System health on the dashboard."]
            self._held = 0
        self.notify(Notification(
            incident_id=reference, severity=severity, title=title, message="\n".join(lines + [DISCLAIMER]),
            priority={"info": 2, "low": 3, "medium": 4, "high": 5}[severity], camera_ids=camera_ids, timestamp_ms=now_ms, kind="health",
        ))
        return True


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def _duration(ms: int) -> str:
    seconds = max(0, int(ms / 1000))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"
