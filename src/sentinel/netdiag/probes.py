"""Network probes behind a small backend interface.

`SystemNetwork` talks to the real network; `simulated.SimulatedNetwork` answers
the same calls from a scenario file, which is how diagnosis is tested. Every
probe is read-only: TCP connects that are closed immediately, one ICMP echo,
multicast discovery, and one unauthenticated HTTP GET."""

from __future__ import annotations

import ctypes
import errno
import http.client
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from .. import onboarding, onvif
from . import collect, sadp
from .model import NetworkSnapshot, PingResult

_NO_ROUTE = {errno.ENETUNREACH, 10051}
_HOST_UNREACHABLE = {errno.EHOSTUNREACH, 10065}


class NetworkBackend(Protocol):
    def snapshot(self, refresh: bool = False) -> NetworkSnapshot: ...
    def tcp_connect(self, host: str, port: int, timeout: float = 1.5) -> str: ...
    def ping(self, host: str, timeout: float = 1.0) -> PingResult: ...
    def discover_sadp(self, snapshot: NetworkSnapshot, timeout: float = 2.5) -> list[dict[str, Any]]: ...
    def discover_onvif(self, snapshot: NetworkSnapshot, timeout: float = 2.5) -> list[dict[str, Any]]: ...
    def http_identify(self, host: str, port: int = 80) -> dict[str, Any] | None: ...
    def rtsp_probe(self, url: str) -> dict[str, Any]: ...


class SystemNetwork:
    """The real network of the computer Sentinel runs on."""

    def __init__(self, cache_s: float = 30.0) -> None:
        self.cache_s = cache_s
        self._snapshot: NetworkSnapshot | None = None
        self._lock = threading.Lock()

    def snapshot(self, refresh: bool = False) -> NetworkSnapshot:
        with self._lock:
            if refresh or self._snapshot is None or time.time() - self._snapshot.collected_at > self.cache_s:
                self._snapshot = collect.collect()
            return self._snapshot

    def tcp_connect(self, host: str, port: int, timeout: float = 1.5) -> str:
        """open | refused (the host is up, the port is closed) | timeout | no_route | host_unreachable | error"""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return "open"
        except ConnectionRefusedError:
            return "refused"
        except (socket.timeout, TimeoutError):
            return "timeout"
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            if code in _NO_ROUTE:
                return "no_route"
            if code in _HOST_UNREACHABLE:
                return "host_unreachable"
            return "error"

    def ping(self, host: str, timeout: float = 1.0) -> PingResult:
        try:
            if sys.platform == "win32":
                return _icmp_windows(host, int(timeout * 1000))
            return _ping_command(host, timeout)
        except Exception:  # diagnostics must not fail because ping is unavailable
            return PingResult("unavailable")

    def discover_sadp(self, snapshot: NetworkSnapshot, timeout: float = 2.5) -> list[dict[str, Any]]:
        return sadp.discover([str(i.ip) for _, i in snapshot.local_networks()], timeout)

    def discover_onvif(self, snapshot: NetworkSnapshot, timeout: float = 2.5) -> list[dict[str, Any]]:
        devices = onvif.discover(timeout, [str(i.ip) for _, i in snapshot.local_networks()])
        return [{"host": d["host"], "method": "onvif", "name": d.get("name") or None, "model": d.get("hardware") or None,
                 "xaddr": d["xaddr"]} for d in devices if d.get("host")]

    def http_identify(self, host: str, port: int = 80) -> dict[str, Any] | None:
        return http_identify(host, port)

    def rtsp_probe(self, url: str) -> dict[str, Any]:
        return onboarding.probe_rtsp(url, timeout=4.0)


# ---- ICMP ---------------------------------------------------------------------------


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [("Address", ctypes.c_uint32), ("Status", ctypes.c_uint32), ("RoundTripTime", ctypes.c_uint32),
                ("DataSize", ctypes.c_uint16), ("Reserved", ctypes.c_uint16), ("Data", ctypes.c_void_p),
                ("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte), ("OptionsSize", ctypes.c_ubyte),
                ("OptionsData", ctypes.c_void_p)]


_ICMP_STATUS = {0: "reply", 11002: "net_unreachable", 11003: "host_unreachable", 11010: "timeout", 11013: "timeout"}


def _icmp_windows(host: str, timeout_ms: int) -> PingResult:
    """IcmpSendEcho (iphlpapi): no admin rights, and status codes instead of
    localized `ping` output, so "host unreachable" and "timed out" stay distinct."""
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
    iphlpapi.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
    handle = iphlpapi.IcmpCreateFile()
    try:
        payload = b"sentinel"
        buffer = ctypes.create_string_buffer(ctypes.sizeof(_IcmpEchoReply) + len(payload) + 64)
        address = struct.unpack("<I", socket.inet_aton(host))[0]
        count = iphlpapi.IcmpSendEcho(handle, address, payload, len(payload), None, buffer, len(buffer), timeout_ms)
        if count == 0:
            return PingResult(_ICMP_STATUS.get(ctypes.get_last_error(), "timeout"))
        reply = _IcmpEchoReply.from_buffer(buffer)
        source = socket.inet_ntoa(struct.pack("<I", reply.Address))
        kind = _ICMP_STATUS.get(reply.Status, "failure")
        return PingResult(kind, source, reply.RoundTripTime if kind == "reply" else None)
    finally:
        iphlpapi.IcmpCloseHandle(handle)


def _ping_command(host: str, timeout: float) -> PingResult:
    completed = subprocess.run(["ping", "-c", "1", "-W", str(max(1, round(timeout))), host], capture_output=True, text=True,
                               timeout=timeout + 3, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    return parse_ping_output(completed.stdout)


def parse_ping_output(text: str) -> PingResult:
    reply = re.search(r"bytes from ([\d.]+).*time=([\d.]+)", text)
    if reply:
        return PingResult("reply", reply.group(1), round(float(reply.group(2))))
    unreachable = re.search(r"From ([\d.]+).*(Host|Net) Unreachable", text, re.I)
    if unreachable:
        return PingResult("host_unreachable" if unreachable.group(2).lower() == "host" else "net_unreachable", unreachable.group(1))
    return PingResult("timeout")


# ---- HTTP identification -----------------------------------------------------------------


def http_identify(host: str, port: int = 80, timeout: float = 3.0) -> dict[str, Any] | None:
    """One unauthenticated GET /. Sends no credentials, so it can never count as a
    failed login. Returns only what identifies the product."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/", headers={"User-Agent": "Sentinel"})
        response = connection.getresponse()
        body = response.read(16384).decode("latin-1", "replace")
        title = re.search(r"<title[^>]*>([^<]{0,80})", body, re.I)
        return {
            "status": response.status,
            "server": (response.getheader("Server") or "")[:60],
            "auth_scheme": (response.getheader("WWW-Authenticate") or "").split(" ", 1)[0][:20],
            "title": title.group(1).strip() if title else "",
            "markers": [marker for marker in _BODY_MARKERS if marker.lower() in body.lower() or marker.lower() in (response.getheader("Location") or "").lower()],
        }
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


_BODY_MARKERS = ("doc/page/login.asp", "hikvision", "dahua", "WEB SERVICE", "reolink", "lorex", "uniview", "amcrest", "axis")
_BRAND_SIGNS = {
    "hikvision": {"ports": {8000}, "server": ("app-webs", "dnvrs-webs", "hikvision-webs"), "markers": ("doc/page/login.asp", "hikvision")},
    "dahua": {"ports": {37777}, "server": (), "markers": ("dahua", "WEB SERVICE", "amcrest")},
    "reolink": {"ports": {9000}, "server": (), "markers": ("reolink",)},
    "lorex": {"ports": set(), "server": (), "markers": ("lorex",)},
    "uniview": {"ports": set(), "server": (), "markers": ("uniview",)},
    "axis": {"ports": set(), "server": ("axis",), "markers": ("axis",)},
}
_PORT_NAMES = {8000: "Hikvision SDK port 8000", 37777: "Dahua TCP port 37777", 9000: "Reolink media port 9000"}


def identify(ports: dict[int, str], http: dict[str, Any] | None, sadp_hit: dict[str, Any] | None) -> dict[str, Any] | None:
    """Best guess at the brand, with the evidence. Only SADP makes it certain."""
    if sadp_hit:
        return {"brand": "hikvision", "confidence": "confirmed", "model": sadp_hit.get("model"),
                "evidence": ["Answered Hikvision SADP discovery" + (f" as {sadp_hit['model']}" if sadp_hit.get("model") else "")]}
    scores: dict[str, list[str]] = {}
    for brand, signs in _BRAND_SIGNS.items():
        evidence = [f"{_PORT_NAMES[p]} is open" for p in sorted(signs["ports"]) if ports.get(p) == "open"]
        if http:
            server = http.get("server", "").lower()
            evidence += [f"web server identifies as '{http['server']}'" for s in signs["server"] if s in server][:1]
            evidence += [f"web page contains '{m}'" for m in signs["markers"] if m in http.get("markers", [])][:1]
        if evidence:
            scores[brand] = evidence
    if not scores:
        return None
    brand = max(scores, key=lambda b: len(scores[b]))
    return {"brand": brand, "confidence": "likely", "model": None, "evidence": scores[brand]}


def parallel(function, items, workers: int = 16) -> list[Any]:
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items) or 1))) as pool:
        return list(pool.map(function, items))
