"""Minimal ONVIF client: WS-Discovery, device info, media profiles, stream URIs.

Standard library only. Covers Profile S basics, which is what onboarding
needs; anything a camera does not support raises OnvifError so the wizard can
fall back to brand templates, HTTP snapshots, or manual paths. Not every DVR
implements ONVIF; never treat its absence as a broken camera."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

MULTICAST = ("239.255.255.250", 3702)
PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{id}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"


class OnvifError(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element, name: str):
    return next((child for child in element.iter() if _local(child.tag) == name), None)


def _text(element, name: str) -> str:
    found = _find(element, name)
    return (found.text or "").strip() if found is not None else ""


def parse_probe_matches(payload: bytes) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(payload)
    devices = []
    for match in (child for child in root.iter() if _local(child.tag) == "ProbeMatch"):
        xaddrs = _text(match, "XAddrs").split()
        scopes = _text(match, "Scopes").split()
        scope = lambda key: next((s.rsplit("/", 1)[-1] for s in scopes if f"/{key}/" in s), "")
        if xaddrs:
            devices.append({
                "xaddr": xaddrs[0],
                "host": urlsplit(xaddrs[0]).hostname,
                "name": urllib.request.unquote(scope("name")),
                "hardware": urllib.request.unquote(scope("hardware")),
            })
    return devices


def discover(timeout: float = 3.0) -> list[dict[str, Any]]:
    """WS-Discovery probe on the local network segment (multicast, UDP 3702)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    try:
        sock.sendto(PROBE.format(id=uuid.uuid4()).encode(), MULTICAST)
        found: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                for device in parse_probe_matches(payload):
                    found.setdefault(device["xaddr"], device)
            except ElementTree.ParseError:
                continue
        return list(found.values())
    finally:
        sock.close()


class OnvifClient:
    def __init__(self, xaddr: str, username: str = "", password: str = "", timeout: float = 6.0) -> None:
        self.xaddr = xaddr
        self.username = username
        self.password = password
        self.timeout = timeout
        self._clock_offset = timedelta(0)

    def _envelope(self, body: str, secure: bool) -> bytes:
        header = ""
        if secure and self.username:
            nonce = os.urandom(16)
            created = (datetime.now(timezone.utc) + self._clock_offset).strftime("%Y-%m-%dT%H:%M:%SZ")
            digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
            header = (
                '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
                f"<UsernameToken><Username>{_escape(self.username)}</Username>"
                '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
                f"{digest}</Password>"
                '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
                f"{base64.b64encode(nonce).decode()}</Nonce>"
                f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
                "</UsernameToken></Security></s:Header>"
            )
        return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">{header}<s:Body>{body}</s:Body></s:Envelope>'.encode()

    def call(self, url: str, body: str, secure: bool = True):
        request = urllib.request.Request(url, data=self._envelope(body, secure), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return ElementTree.fromstring(response.read())
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", "replace")
            if error.code in (400, 401, 403, 500) and ("NotAuthorized" in text or "Authority" in text or error.code == 401):
                raise OnvifError("auth_failed") from None
            raise OnvifError(f"http_{error.code}") from None
        except (urllib.error.URLError, OSError):
            raise OnvifError("unreachable") from None
        except ElementTree.ParseError:
            raise OnvifError("not_onvif") from None

    def sync_clock(self) -> None:
        """WS-Security timestamps fail when the camera clock is off; measure the skew."""
        try:
            root = self.call(self.xaddr, f'<GetSystemDateAndTime xmlns="{DEVICE_NS}"/>', secure=False)
        except OnvifError:
            return
        utc = _find(root, "UTCDateTime")
        if utc is None:
            return
        try:
            camera = datetime(
                int(_text(utc, "Year")), int(_text(utc, "Month")), int(_text(utc, "Day")),
                int(_text(utc, "Hour")), int(_text(utc, "Minute")), int(_text(utc, "Second")), tzinfo=timezone.utc,
            )
        except ValueError:
            return
        self._clock_offset = camera - datetime.now(timezone.utc)

    def device_info(self) -> dict[str, str]:
        root = self.call(self.xaddr, f'<GetDeviceInformation xmlns="{DEVICE_NS}"/>')
        return {key: _text(root, key) for key in ("Manufacturer", "Model", "FirmwareVersion")}

    def media_xaddr(self) -> str:
        root = self.call(self.xaddr, f'<GetCapabilities xmlns="{DEVICE_NS}"><Category>Media</Category></GetCapabilities>')
        media = _find(root, "Media")
        address = _text(media, "XAddr") if media is not None else ""
        if not address:
            raise OnvifError("no_media_service")
        return address

    def profiles(self, media: str) -> list[dict[str, Any]]:
        root = self.call(media, f'<GetProfiles xmlns="{MEDIA_NS}"/>')
        result = []
        for profile in (child for child in root.iter() if _local(child.tag) == "Profiles"):
            encoder = _find(profile, "VideoEncoderConfiguration")
            if encoder is None:
                continue
            result.append({
                "token": profile.get("token", ""),
                "name": _text(profile, "Name"),
                "encoding": _text(encoder, "Encoding"),
                "width": int(_text(encoder, "Width") or 0),
                "height": int(_text(encoder, "Height") or 0),
                "fps": float(_text(encoder, "FrameRateLimit") or 0) or None,
            })
        return result

    def stream_uri(self, media: str, token: str) -> str:
        body = (
            f'<GetStreamUri xmlns="{MEDIA_NS}"><StreamSetup><Stream xmlns="{SCHEMA_NS}">RTP-Unicast</Stream>'
            f'<Transport xmlns="{SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
            f"<ProfileToken>{_escape(token)}</ProfileToken></GetStreamUri>"
        )
        uri = _text(self.call(media, body), "Uri")
        if not uri:
            raise OnvifError("no_stream_uri")
        return uri


def choose_streams(profiles: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Main = largest picture; sub = smallest that is still at least 320 px wide."""
    ranked = sorted(profiles, key=lambda p: p["width"] * p["height"], reverse=True)
    main = ranked[0]
    candidates = [p for p in ranked[1:] if p["width"] >= 320 and p["width"] * p["height"] < main["width"] * main["height"]]
    return main, (candidates[-1] if candidates else None)


def inspect(xaddr: str, username: str, password: str) -> dict[str, Any]:
    """Everything the wizard needs from one ONVIF camera. The returned stream
    entries carry host/port/path only; credentials stay with the caller."""
    client = OnvifClient(xaddr, username, password)
    client.sync_clock()
    info = client.device_info()
    media = client.media_xaddr()
    profiles = client.profiles(media)
    if not profiles:
        raise OnvifError("no_video_profiles")
    main, sub = choose_streams(profiles)
    streams = {}
    for which, profile in (("main", main), ("sub", sub)):
        if profile is None:
            continue
        parts = urlsplit(client.stream_uri(media, profile["token"]))
        streams[which] = {
            "host": parts.hostname, "port": parts.port or 554,
            "path": parts.path + (f"?{parts.query}" if parts.query else ""),
            "profile": {key: profile[key] for key in ("name", "encoding", "width", "height", "fps")},
        }
    return {"device": info, "profiles": profiles, "streams": streams}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
