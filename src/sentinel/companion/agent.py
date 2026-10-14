"""Companion health agent: pairs with the Hub, holds an authenticated session,
and reports device health, the MAVLink relay's state and video availability.

It never talks to the flight controller and never acts on the aircraft. If the
Hub is unreachable it keeps relaying (the pilot's ground station is not
affected), retries with backoff, and reports again when the Hub is back.
Standard library only, so it runs on a Raspberry Pi OS Lite image as is."""

from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..drone.pairing import session_mac

log = logging.getLogger("sentinel.companion.agent")


class HubError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status


def _post(url: str, body: dict[str, Any], token: str = "", timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"content-type": "application/json"})
    if token:
        request.add_header("authorization", f"Companion {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}").get("detail", "")
        except ValueError:
            detail = ""
        raise HubError(error.code, str(detail)[:200]) from None


def save_identity(path: Path, identity: dict[str, Any]) -> None:
    """The identity holds the companion secret: owner-only file permissions.
    A product unit keeps it in a secure element instead (see docs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(identity, handle)
    os.replace(temporary, path)


def load_identity(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def pair(hub_url: str, code: str, name: str, identity_path: Path, *, hardware: dict[str, Any] | None = None) -> dict[str, Any]:
    result = _post(f"{hub_url.rstrip('/')}/api/v1/drone/companion/pair", {"code": code, "name": name, "hardware": hardware or device_hardware()})
    identity = {"hub_url": hub_url.rstrip("/"), "companion_id": result["companion_id"], "drone_id": result["drone_id"], "secret": result["secret"]}
    save_identity(identity_path, identity)
    return {"companion_id": identity["companion_id"], "drone_id": identity["drone_id"]}


def device_hardware() -> dict[str, Any]:
    model = ""
    for candidate in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            model = Path(candidate).read_text(errors="replace").strip("\x00\n")
            break
        except OSError:
            continue
    return {"model": model or platform.machine(), "os": platform.platform()[:80], "python": platform.python_version()}


def device_health() -> dict[str, Any]:
    health: dict[str, Any] = {"uptime_s": None, "cpu_temp_c": None, "load_1m": None, "memory_free_mb": None, "disk_free_mb": None}
    try:
        health["uptime_s"] = round(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        health["cpu_temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    if hasattr(os, "getloadavg"):
        health["load_1m"] = round(os.getloadavg()[0], 2)
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                health["memory_free_mb"] = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    health["disk_free_mb"] = shutil.disk_usage(Path.home()).free // (1024 * 1024)
    return health


def probe_video(url: str, timeout: float = 1.0) -> dict[str, Any]:
    """Is the RTSP server accepting connections? (No credentials are used here.)"""
    if not url:
        return {"configured": False}
    host_port = url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection((host, int(port or 554)), timeout=timeout):
            return {"configured": True, "reachable": True}
    except OSError as error:
        return {"configured": True, "reachable": False, "error": type(error).__name__}


class CompanionAgent:
    def __init__(self, identity: dict[str, Any], *, relay_status: Callable[[], dict[str, Any]] = lambda: {},
                 video_url: str = "", interval_s: float = 2.0, post: Callable[..., dict[str, Any]] = _post,
                 health: Callable[[], dict[str, Any]] = device_health) -> None:
        self.identity = identity
        self.hub = identity["hub_url"]
        self.relay_status = relay_status
        self.video_url = video_url
        self.interval_s = interval_s
        self.post = post
        self.health = health
        self.token = ""
        self.token_expires_ms = 0
        self.state = "starting"
        self.last_error = ""
        self.reports_sent = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def open_session(self) -> None:
        import base64

        secret = base64.b64decode(self.identity["secret"])
        timestamp = int(time.time() * 1000)
        nonce = secrets.token_hex(12)
        result = self.post(f"{self.hub}/api/v1/drone/companion/session", {
            "companion_id": self.identity["companion_id"], "timestamp_ms": timestamp, "nonce": nonce,
            "mac": session_mac(secret, self.identity["companion_id"], timestamp, nonce)})
        self.token, self.token_expires_ms = result["token"], int(result["expires_ms"])

    def report_once(self) -> None:
        if not self.token or time.time() * 1000 > self.token_expires_ms - 60_000:
            self.open_session()
        body = {"health": self.health(), "relay": self.relay_status(), "video": probe_video(self.video_url), "sent_ms": int(time.time() * 1000)}
        try:
            self.post(f"{self.hub}/api/v1/drone/companion/report", body, self.token)
        except HubError as error:
            if error.status != 401:
                raise
            self.open_session()  # Hub restarted or the session expired
            self.post(f"{self.hub}/api/v1/drone/companion/report", body, self.token)
        self.reports_sent += 1

    def start(self) -> "CompanionAgent":
        self._thread = threading.Thread(target=self._run, name="companion-agent", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        backoff = self.interval_s
        while not self._stop.is_set():
            try:
                self.report_once()
                self.state, self.last_error, backoff = "reporting", "", self.interval_s
            except HubError as error:
                self.state, self.last_error = ("revoked" if error.status in (401, 403) else "hub_error"), str(error)
                backoff = min(60.0, backoff * 2)
            except (OSError, ValueError) as error:  # Hub unreachable: keep relaying, retry later
                self.state, self.last_error = "hub_unreachable", f"{type(error).__name__}: {error}"[:200]
                backoff = min(60.0, backoff * 2)
            self._stop.wait(backoff)
