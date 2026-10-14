"""Hikvision SADP discovery (multicast 239.255.255.250:37020).

SADP is what Hikvision's own SADP tool uses. Its answers are multicast too, so it
finds Hikvision devices on the same cable network *even when their IP settings
belong to a different network*: exactly the case where ping, the browser and
ONVIF (whose replies are unicast) all fail. Serial numbers and MAC addresses in
the reply are deliberately dropped."""

from __future__ import annotations

import select
import socket
import time
import uuid
import xml.etree.ElementTree as ElementTree
from typing import Any

GROUP, PORT = "239.255.255.250", 37020
PROBE = '<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{id}</Uuid><Types>inquiry</Types></Probe>'


def parse_reply(payload: bytes) -> dict[str, Any] | None:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1] != "ProbeMatch":
        return None
    text = lambda name: (root.findtext(name) or "").strip()
    host = text("IPv4Address")
    if not host:
        return None
    number = lambda name: int(text(name)) if text(name).isdigit() else None
    flag = lambda name: {"true": True, "false": False}.get(text(name).lower())
    return {
        "host": host, "method": "sadp", "brand": "hikvision",
        "model": text("DeviceDescription") or text("DeviceType"),
        "mask": text("IPv4SubnetMask") or None, "gateway": text("IPv4Gateway") or None,
        "dhcp": flag("DHCP"), "activated": flag("Activated"),
        "http_port": number("HttpPort"), "command_port": number("CommandPort"),
        "channels": (number("DigitalChannelNum") or 0) + (number("AnalogChannelNum") or 0) or None,
        "firmware": text("SoftwareVersion") or None,
    }


def discover(local_ips: list[str], timeout: float = 2.5) -> list[dict[str, Any]]:
    """Send one inquiry out of each local interface and collect replies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", PORT))  # replies go to the group port
        except OSError:
            sock.bind(("", 0))  # e.g. Hikvision's SADP tool is running: unicast replies only
        for ip in local_ips:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(ip))
            except OSError:
                pass
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        probe_id = str(uuid.uuid4()).upper()
        for ip in local_ips:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                sock.sendto(PROBE.format(id=probe_id).encode(), (GROUP, PORT))
            except OSError:
                continue
        found: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([sock], [], [], min(remaining, 0.3))
            if not ready:
                continue
            payload, _ = sock.recvfrom(65535)
            device = parse_reply(payload)
            if device:
                found.setdefault(device["host"], device)
        return list(found.values())
    finally:
        sock.close()
