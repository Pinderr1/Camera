"""MAVLink relay on the companion computer (prototype; standard library only).

    flight controller  <->  relay  <->  Sentinel Hub (UDP)
                                   <->  pilot ground station(s) (UDP)

Everything from the flight controller goes to the Hub and the ground
stations. Traffic from the pilot's ground station goes to the flight
controller unchanged: the pilot is in command. Traffic from the Hub is
filtered by policy:

- `telemetry` (default, physical aircraft): only CRC-valid frames of a short
  read-only allowlist pass (heartbeat, parameter and mission *reads*, stream
  rate requests, and COMMAND_LONG with telemetry-only commands). Anything
  that could change flight (mode changes, arming, take-off, position targets,
  parameter writes, mission uploads) is dropped and counted.
- `sitl`: everything passes. Allowed only when the flight-controller endpoint
  is a loopback address (a simulator on this machine).

This is a second, independent layer behind the Hub's own refusal. A product
build replaces the relay with mavlink-router plus MAVLink 2 signing (see
docs/drone-integration.md); the policy stays the same.

`set_hub_link(False)` drops all Hub traffic in both directions, which is how
tests and demos simulate losing the radio link to the Hub."""

from __future__ import annotations

import ipaddress
import logging
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("sentinel.companion.relay")

MAGIC_V1, MAGIC_V2 = 0xFE, 0xFD
# Hub -> FC allowlist: message id -> (CRC_EXTRA, minimum wire length)
HUB_ALLOWED = {
    0: (50, 9),     # HEARTBEAT
    2: (137, 12),   # SYSTEM_TIME
    20: (214, 20),  # PARAM_REQUEST_READ
    21: (159, 2),   # PARAM_REQUEST_LIST
    43: (132, 2),   # MISSION_REQUEST_LIST (read)
    47: (153, 3),   # MISSION_ACK (ends a read)
    51: (196, 4),   # MISSION_REQUEST_INT (read)
    66: (148, 6),   # REQUEST_DATA_STREAM (telemetry rate)
    76: (152, 33),  # COMMAND_LONG (command id checked below)
    111: (34, 16),  # TIMESYNC
}
COMMAND_LONG_ID = 76
TELEMETRY_COMMANDS = {511, 512, 519, 520}  # SET_MESSAGE_INTERVAL, REQUEST_MESSAGE, REQUEST_PROTOCOL_VERSION, REQUEST_AUTOPILOT_CAPABILITIES


def x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


@dataclass
class Frame:
    raw: bytes
    msgid: int
    sysid: int
    compid: int
    payload: bytes
    version: int


class FrameParser:
    """Splits a byte stream into MAVLink v1/v2 frames (no CRC check here)."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.garbage = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer += data
        frames = []
        while True:
            start = next((i for i, b in enumerate(self.buffer) if b in (MAGIC_V1, MAGIC_V2)), None)
            if start is None:
                self.garbage += len(self.buffer)
                self.buffer.clear()
                return frames
            if start:
                self.garbage += start
                del self.buffer[:start]
            if len(self.buffer) < 3:
                return frames
            length = self.buffer[1]
            if self.buffer[0] == MAGIC_V2:
                signed = len(self.buffer) > 2 and self.buffer[2] & 0x01
                total = 12 + length + (13 if signed else 0)
                if len(self.buffer) < total:
                    return frames
                raw = bytes(self.buffer[:total])
                msgid = raw[7] | raw[8] << 8 | raw[9] << 16
                frames.append(Frame(raw, msgid, raw[5], raw[6], raw[10:10 + length], 2))
            else:
                total = 8 + length
                if len(self.buffer) < total:
                    return frames
                raw = bytes(self.buffer[:total])
                frames.append(Frame(raw, raw[5], raw[3], raw[4], raw[6:6 + length], 1))
            del self.buffer[:total]


def crc_ok(frame: Frame, crc_extra: int) -> bool:
    length = len(frame.payload)
    header_end = 10 if frame.version == 2 else 6
    body = frame.raw[1:header_end + length]
    expected = x25_crc(bytes([crc_extra]), x25_crc(body))
    received = struct.unpack_from("<H", frame.raw, header_end + length)[0]
    return expected == received


def hub_frame_allowed(frame: Frame) -> tuple[bool, str]:
    rule = HUB_ALLOWED.get(frame.msgid)
    if rule is None:
        return False, f"message {frame.msgid} not allowed from the Hub"
    if not crc_ok(frame, rule[0]):
        return False, "bad checksum"
    if frame.msgid == COMMAND_LONG_ID:
        payload = frame.payload + bytes(max(0, rule[1] - len(frame.payload)))  # MAVLink 2 trims trailing zeros
        command = struct.unpack_from("<H", payload, 28)[0]
        if command not in TELEMETRY_COMMANDS:
            return False, f"command {command} not allowed from the Hub"
    return True, ""


def _is_loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class RelayStats:
    fc_frames: int = 0
    hub_frames_in: int = 0
    hub_frames_forwarded: int = 0
    hub_frames_dropped: int = 0
    gcs_frames: int = 0
    last_drop_reason: str = ""
    fc_connected: bool = False
    fc_reconnects: int = 0
    last_fc_ms: int = 0
    last_hub_ms: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)


class MavlinkRelay:
    def __init__(self, fc: str, hub: str, gcs: list[str] | None = None, *, policy: str = "telemetry") -> None:
        if policy not in ("telemetry", "sitl"):
            raise ValueError("policy must be telemetry or sitl")
        scheme, host, _ = _parse(fc)
        if policy == "sitl" and not (scheme in ("tcp", "udp", "udpin") and _is_loopback(host)):
            raise ValueError("the sitl policy needs a loopback flight-controller endpoint (a simulator on this machine)")
        self.fc_endpoint = fc
        self.hub_address = _address(hub)
        self.gcs_addresses = [_address(item) for item in (gcs or [])]
        self.policy = policy
        self.stats = RelayStats()
        self.hub_link_up = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fc_sock: socket.socket | None = None
        self._fc_peer: tuple[str, int] | None = None
        self._hub_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._hub_sock.bind(("0.0.0.0" if not _is_loopback(self.hub_address[0]) else "127.0.0.1", 0))
        self._gcs_socks = []
        for address in self.gcs_addresses:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1" if _is_loopback(address[0]) else "0.0.0.0", 0))
            self._gcs_socks.append((sock, address))
        self._hub_parser = FrameParser()

    def set_hub_link(self, up: bool) -> None:
        self.hub_link_up = up

    def start(self) -> "MavlinkRelay":
        self._thread = threading.Thread(target=self._run, name="companion-relay", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        for sock in [self._fc_sock, self._hub_sock] + [s for s, _ in self._gcs_socks]:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def status(self) -> dict[str, Any]:
        now = int(time.time() * 1000)
        stats = self.stats
        return {"policy": self.policy, "fc_connected": stats.fc_connected, "fc_reconnects": stats.fc_reconnects,
                "fc_frames": stats.fc_frames, "hub_frames_in": stats.hub_frames_in, "hub_frames_forwarded": stats.hub_frames_forwarded,
                "hub_frames_dropped": stats.hub_frames_dropped, "dropped_by_reason": dict(list(stats.dropped_by_reason.items())[:10]),
                "gcs_frames": stats.gcs_frames, "hub_link_up": self.hub_link_up,
                "last_fc_age_s": round((now - stats.last_fc_ms) / 1000, 1) if stats.last_fc_ms else None,
                "last_hub_age_s": round((now - stats.last_hub_ms) / 1000, 1) if stats.last_hub_ms else None}

    # ---- internals ---------------------------------------------------------

    def _open_fc(self) -> None:
        scheme, host, port = _parse(self.fc_endpoint)
        if scheme == "tcp":
            sock = socket.create_connection((host, port), timeout=3)
            sock.settimeout(None)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if scheme == "udpin":
                sock.bind((host, port))
            else:
                sock.bind(("127.0.0.1" if _is_loopback(host) else "0.0.0.0", 0))
                self._fc_peer = (host, port)
        self._fc_sock = sock
        self.stats.fc_connected = True

    def _write_fc(self, data: bytes) -> None:
        sock = self._fc_sock
        if sock is None:
            return
        scheme, _, _ = _parse(self.fc_endpoint)
        if scheme == "tcp":
            sock.sendall(data)
        elif self._fc_peer:
            sock.sendto(data, self._fc_peer)

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            if self._fc_sock is None:
                try:
                    self._open_fc()
                    backoff = 0.5
                except OSError as error:
                    self.stats.fc_connected = False
                    log.warning("flight controller %s unreachable: %s", self.fc_endpoint, error)
                    self._stop.wait(backoff)
                    backoff = min(10.0, backoff * 2)
                    continue
            readable = [self._fc_sock, self._hub_sock] + [s for s, _ in self._gcs_socks]
            try:
                ready, _, _ = select.select(readable, [], [], 0.2)
            except (OSError, ValueError):
                self._drop_fc()
                continue
            for sock in ready:
                try:
                    if sock is self._fc_sock:
                        self._from_fc(sock)
                    elif sock is self._hub_sock:
                        self._from_hub(sock)
                    else:
                        self._from_gcs(sock)
                except OSError as error:
                    if sock is self._fc_sock:
                        log.warning("flight controller link lost: %s", error)
                        self._drop_fc()
                        break

    def _drop_fc(self) -> None:
        if self._fc_sock is not None:
            try:
                self._fc_sock.close()
            except OSError:
                pass
        self._fc_sock = None
        self.stats.fc_connected = False
        self.stats.fc_reconnects += 1

    def _from_fc(self, sock: socket.socket) -> None:
        scheme, _, _ = _parse(self.fc_endpoint)
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
        if self.hub_link_up:
            self._hub_sock.sendto(data, self.hub_address)
        for gcs_sock, address in self._gcs_socks:
            gcs_sock.sendto(data, address)

    def _from_hub(self, sock: socket.socket) -> None:
        data, _ = sock.recvfrom(65536)
        if not self.hub_link_up:
            return
        self.stats.last_hub_ms = int(time.time() * 1000)
        for frame in self._hub_parser.feed(data):
            self.stats.hub_frames_in += 1
            allowed, reason = (True, "") if self.policy == "sitl" else hub_frame_allowed(frame)
            if allowed:
                self._write_fc(frame.raw)
                self.stats.hub_frames_forwarded += 1
            else:
                self.stats.hub_frames_dropped += 1
                self.stats.last_drop_reason = reason
                key = reason.split(" not allowed")[0] if "not allowed" in reason else reason
                self.stats.dropped_by_reason[key] = self.stats.dropped_by_reason.get(key, 0) + 1

    def _from_gcs(self, sock: socket.socket) -> None:
        data, _ = sock.recvfrom(65536)
        self.stats.gcs_frames += 1
        self._write_fc(data)  # the pilot's ground station is in command


def _parse(endpoint: str) -> tuple[str, str, int]:
    parts = endpoint.split(":")
    if len(parts) != 3 or parts[0] not in ("tcp", "udp", "udpin"):
        raise ValueError(f"endpoint must be tcp:HOST:PORT, udp:HOST:PORT or udpin:HOST:PORT, got {endpoint!r}")
    return parts[0], parts[1], int(parts[2])


def _address(endpoint: str) -> tuple[str, int]:
    scheme, host, port = _parse(endpoint)
    if scheme != "udp":
        raise ValueError(f"Hub and ground-station endpoints are udp:HOST:PORT, got {endpoint!r}")
    return host, port
