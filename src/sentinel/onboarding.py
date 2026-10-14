"""Camera onboarding: connection tests, diagnostics, and local device discovery.

`probe_rtsp` speaks just enough RTSP (DESCRIBE with Basic/Digest auth) to tell
apart the failures installers actually hit: wrong IP, closed port, wrong
password, wrong stream path, and codecs the decoder can't handle. Messages and
results never contain URLs or passwords."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import unquote, urlsplit

from .streams import BRAND_TEMPLATES, SecretLookup, build_url, has_substream, scrub, selected_stream

ADVICE = {
    "ok": "Connected.",
    "dns_failed": "The camera's host name could not be resolved. Use its IP address.",
    "unreachable": "Nothing answered at that address. Check the IP, that the camera is powered, and that this computer is on the same network.",
    "port_closed": "The device answered but the stream port is closed. Enable RTSP in the camera/DVR settings or check the port number.",
    "auth_required": "The camera needs a username and password.",
    "auth_failed": "Username or password rejected. Some DVRs lock accounts after repeated failures; check the credentials before retrying.",
    "not_found": "Login worked but that stream path does not exist. Try another brand template or channel number.",
    "not_rtsp": "Something answered on that port, but it is not an RTSP server.",
    "open_failed": "The decoder could not open the stream.",
    "no_frames": "The stream opened but delivered no images. The codec may be unsupported (for example H.265 without decoder support).",
    "timeout": "The connection test timed out.",
    "device_unavailable": "The capture device could not be opened. It may be in use by another program or unplugged.",
}


# ---- RTSP-level probe --------------------------------------------------------


def probe_rtsp(url: str, timeout: float = 5.0) -> dict[str, Any]:
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 554
    username, password = unquote(parts.username or ""), unquote(parts.password or "")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    request_uri = f"rtsp://{host}:{port}{path}"
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
    except socket.gaierror:
        return _result("dns_failed")
    except ConnectionRefusedError:
        return _result("port_closed")
    except OSError:
        return _result("unreachable")
    with connection:
        connection.settimeout(timeout)
        try:
            status, headers, body = _describe(connection, request_uri, 1, None)
            if status == 401:
                if not username:
                    return _result("auth_required")
                auth = _authorization(headers, username, password, request_uri)
                if auth is None:
                    return _result("auth_failed", "unsupported authentication scheme")
                status, headers, body = _describe(connection, request_uri, 2, auth)
                if status == 401:
                    return _result("auth_failed")
        except (socket.timeout, TimeoutError):
            return _result("timeout")
        except (OSError, ValueError):
            return _result("not_rtsp")
    if status == 200:
        return _result("ok") | {"codec": _sdp_codec(body)}
    if status in (404, 454):
        return _result("not_found")
    return _result(f"rtsp_{status}", f"The camera answered RTSP status {status}.")


def _describe(connection: socket.socket, uri: str, cseq: int, auth: str | None) -> tuple[int, dict[str, list[str]], str]:
    lines = [f"DESCRIBE {uri} RTSP/1.0", f"CSeq: {cseq}", "Accept: application/sdp", "User-Agent: Sentinel"]
    if auth:
        lines.append(f"Authorization: {auth}")
    connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    header_lines = head.decode("latin-1").split("\r\n")
    match = re.match(r"RTSP/1\.\d (\d{3})", header_lines[0] if header_lines else "")
    if not match:
        raise ValueError("not an RTSP response")
    headers: dict[str, list[str]] = {}
    for line in header_lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers.setdefault(key.strip().lower(), []).append(value.strip())
    length = int((headers.get("content-length") or ["0"])[0])
    while len(rest) < length:
        chunk = connection.recv(4096)
        if not chunk:
            break
        rest += chunk
    return int(match.group(1)), headers, rest[:length].decode("latin-1", "replace")


def _authorization(headers: dict[str, list[str]], username: str, password: str, uri: str) -> str | None:
    challenges = headers.get("www-authenticate", [])
    digest = next((c for c in challenges if c.lower().startswith("digest")), None)
    if digest:
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', digest[6:]))
        realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}"'
    if any(c.lower().startswith("basic") for c in challenges):
        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    return None


def _sdp_codec(sdp: str) -> str | None:
    in_video = False
    for line in sdp.splitlines():
        if line.startswith("m="):
            in_video = line.startswith("m=video")
        elif in_video and line.startswith("a=rtpmap:"):
            return line.split()[1].split("/")[0].upper()
    return None


def _result(kind: str, message: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "ok": kind == "ok", "message": message or ADVICE.get(kind, kind)}


# ---- decoder-level test -------------------------------------------------------


def open_capture(target: Any, *, hw_decode: str = "auto", backend: str = "auto", resolution: list[int] | None = None,
                 open_timeout_ms: int = 8000):
    import cv2

    if isinstance(target, int):
        api = {"dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "v4l2": cv2.CAP_V4L2}.get(backend, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
        capture = cv2.VideoCapture(target, api)
        if capture.isOpened() and resolution:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, resolution[0])
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, resolution[1])
        return capture, False
    timeouts = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000]
    if hw_decode != "off":
        capture = cv2.VideoCapture(target, cv2.CAP_FFMPEG, timeouts + [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
        if capture.isOpened():
            return capture, capture.get(cv2.CAP_PROP_HW_ACCELERATION) > 0
    return cv2.VideoCapture(target, cv2.CAP_FFMPEG, timeouts), False


def measure_stream(target: Any, *, frames: int = 15, max_seconds: float = 6.0, **open_options: Any) -> dict[str, Any]:
    """Open, read a few frames, and report what the decoder actually delivers."""
    import cv2

    capture, hw_active = open_capture(target, **open_options)
    if not capture.isOpened():
        return _result("device_unavailable" if isinstance(target, int) else "open_failed")
    try:
        reported = capture.get(cv2.CAP_PROP_FPS)
        fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
        codec = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ") or None
        started, count, last = time.monotonic(), 0, None
        while count < frames and time.monotonic() - started < max_seconds:
            ok, image = capture.read()
            if not ok:
                break
            count, last = count + 1, image
        elapsed = time.monotonic() - started
    finally:
        capture.release()
    if last is None:
        return _result("no_frames")
    ok, jpeg = cv2.imencode(".jpg", last, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return _result("ok") | {
        "resolution": [int(last.shape[1]), int(last.shape[0])],
        "reported_fps": round(reported, 1) if 0 < reported < 240 else None,
        "measured_fps": round(count / elapsed, 1) if elapsed > 0 and count > 1 else None,
        "codec": codec,
        "hw_decode_active": bool(hw_active),
        "frames_read": count,
        "snapshot_jpeg": jpeg.tobytes() if ok else b"",
    }


def test_source(source: dict[str, Any], secrets: SecretLookup | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    """Full connection test for the wizard. Tests main and sub streams when the
    camera has both, recommends the stream for AI processing, and returns one
    preview frame. Always returns within `timeout` seconds."""
    result: dict[str, Any] = {}

    def work() -> None:
        try:
            result.update(_test(source, secrets))
        except KeyError as error:  # missing credential
            result.update(_result("auth_required", scrub(str(error))))
        except Exception as error:  # report, never raise, from a diagnostics endpoint
            result.update(_result("open_failed", scrub(f"{type(error).__name__}: {error}")))

    thread = threading.Thread(target=work, daemon=True, name="camera-test")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return _result("timeout") | {"streams": {}}
    return result


def _test(source: dict[str, Any], secrets: SecretLookup | None) -> dict[str, Any]:
    kind = source["type"]
    if kind in {"usb", "capture"}:
        stream = measure_stream(int(source.get("index", 0)), backend=source.get("backend", "auto"), resolution=source.get("resolution"))
        return _summary({"main": stream}, "main")
    if kind == "file":
        return _summary({"main": measure_stream(source["path"], hw_decode=source.get("hw_decode", "auto"))}, "main")
    if kind == "simulated":
        return _summary({"main": _result("ok") | {"resolution": [640, 360], "simulated": True}}, "main")
    if kind == "http" and source.get("mode") == "snapshot":
        return _summary({"main": _test_snapshot(build_url(source, "main", secrets))}, "main")
    streams: dict[str, Any] = {}
    for which in (["main", "sub"] if has_substream(source) else ["main"]):
        url = build_url(source, which, secrets)
        probe = probe_rtsp(url) if kind == "rtsp" else _result("ok")
        if probe["ok"]:
            measured = measure_stream(url, hw_decode=source.get("hw_decode", "auto"))
            if probe.get("codec"):  # SDP names the codec more reliably than the decoder's fourcc
                measured["codec"] = probe["codec"]
            streams[which] = measured
        else:
            streams[which] = probe
    preferred = selected_stream(source, "ai")
    if preferred == "sub" and not streams.get("sub", {}).get("ok"):
        preferred = "main"
    return _summary(streams, preferred)


def _test_snapshot(url: str) -> dict[str, Any]:
    import cv2
    import numpy as np

    opener, plain_url = snapshot_opener(url)
    try:
        with opener.open(plain_url, timeout=6) as response:
            data = response.read()
    except Exception as error:
        text = str(error)
        kind = "auth_failed" if "401" in text else "not_found" if "404" in text else "unreachable"
        return _result(kind)
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return _result("no_frames", "The URL answered but did not return a JPEG image.")
    return _result("ok") | {"resolution": [int(image.shape[1]), int(image.shape[0])], "codec": "JPEG", "snapshot_jpeg": data}


def snapshot_opener(url: str):
    """(urllib opener with Basic+Digest auth, URL without credentials)."""
    import urllib.request

    parts = urlsplit(url)
    if not parts.username:
        return urllib.request.build_opener(), url
    bare = parts._replace(netloc=(parts.hostname or "") + (f":{parts.port}" if parts.port else "")).geturl()
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, bare, unquote(parts.username), unquote(parts.password or ""))
    return urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(manager), urllib.request.HTTPDigestAuthHandler(manager)), bare


def _summary(streams: dict[str, Any], preferred: str) -> dict[str, Any]:
    chosen = streams.get(preferred, {})
    ok = bool(chosen.get("ok"))
    diagnostics = [f"{which}: {stream['message']}" for which, stream in streams.items() if not stream.get("ok")]
    for which, stream in streams.items():
        if stream.get("ok") and stream.get("resolution") and stream["resolution"][0] > 1280 and which == "main" and "sub" not in streams:
            diagnostics.append("Only a high-resolution stream is available; decoding it costs much more CPU. Enable a sub-stream in the camera if possible.")
        if stream.get("ok") and str(stream.get("codec", "")).upper() in {"H265", "HEVC", "HEV1", "HVC1"} and not stream.get("hw_decode_active"):
            diagnostics.append(f"{which}: H.265 is decoded in software here; expect higher CPU use.")
    snapshot = chosen.get("snapshot_jpeg", b"")
    clean = {which: {key: value for key, value in stream.items() if key != "snapshot_jpeg"} for which, stream in streams.items()}
    return {
        "ok": ok,
        "kind": chosen.get("kind", "open_failed"),
        "message": chosen.get("message", ""),
        "streams": clean,
        "ai_stream": preferred if ok else None,
        "diagnostics": diagnostics,
        "snapshot_jpeg_b64": base64.b64encode(snapshot).decode("ascii") if snapshot else "",
    }


def scan_channels(source: dict[str, Any], secrets: SecretLookup | None, *, probe=probe_rtsp, max_channels: int = 16) -> dict[str, Any]:
    """Which channels of a DVR/NVR answer, using RTSP DESCRIBE only (no decoding).
    Channel 1 is tried first and the scan stops on a login failure, so a wrong
    password costs one attempt, not one per channel (recorders lock accounts).
    A recorder may also answer for a channel with no camera attached."""
    if source.get("type") != "rtsp" or "{channel" not in BRAND_TEMPLATES.get(source.get("template", ""), {}).get("main", ""):
        return _result("not_found", "Channel search needs an RTSP brand template with channel numbers (for example Hikvision or Dahua).") | {"channels": []}
    first = probe(build_url(dict(source, channel=1), "main", secrets))
    if first["kind"] in {"auth_required", "auth_failed", "unreachable", "port_closed", "dns_failed", "not_rtsp", "timeout"}:
        return first | {"channels": []}

    def check(channel: int) -> dict[str, Any]:
        main = first if channel == 1 else probe(build_url(dict(source, channel=channel), "main", secrets))
        entry = {"channel": channel, "main": {k: main.get(k) for k in ("kind", "codec")}}
        if main["ok"] and "sub" in BRAND_TEMPLATES[source["template"]]:
            sub = probe(build_url(dict(source, channel=channel), "sub", secrets))
            entry["sub"] = {k: sub.get(k) for k in ("kind", "codec")}
        return entry

    with ThreadPoolExecutor(max_workers=4) as pool:
        channels = list(pool.map(check, range(1, max(1, min(int(max_channels), 64)) + 1)))
    found = [c for c in channels if c["main"]["kind"] == "ok"]
    message = f"{len(found)} channel(s) answered." if found else "No channel answered with that stream path. Try another brand template."
    return {"ok": bool(found), "kind": "ok" if found else "not_found", "message": message, "channels": channels}


# ---- discovery helpers --------------------------------------------------------


def list_local_devices(max_index: int = 5) -> list[dict[str, Any]]:
    """USB webcams and video capture devices (including analog-to-USB capture
    cards for older camera systems). Opening a device can briefly turn on its
    light, so this runs only when the user asks."""
    import cv2

    found = []
    for index in range(max_index + 1):
        capture, _ = open_capture(index)
        if capture.isOpened():
            ok, image = capture.read()
            if ok and image is not None:
                found.append({"index": index, "resolution": [int(image.shape[1]), int(image.shape[0])]})
        capture.release()
    return found


def templates() -> list[dict[str, Any]]:
    return [
        {"id": key, "label": value["label"], "port": value["port"], "has_substream": "sub" in value, "has_snapshot": "snapshot" in value}
        for key, value in BRAND_TEMPLATES.items()
    ]


def decode_capabilities() -> dict[str, Any]:
    import cv2

    info = cv2.getBuildInformation()
    ffmpeg = bool(re.search(r"FFMPEG:\s+YES", info))
    api = {"win32": "Direct3D 11 (D3D11VA)", "linux": "VAAPI / CUDA where available", "darwin": "VideoToolbox (not exposed by OpenCV)"}
    return {
        "opencv": cv2.__version__,
        "ffmpeg_backend": ffmpeg,
        "hw_decode_api": api.get(sys.platform, "unknown") if hasattr(cv2, "VIDEO_ACCELERATION_ANY") and ffmpeg else None,
        "note": "Hardware decoding is attempted per stream (hw_decode: auto) with software fallback; each camera's status reports whether it engaged.",
    }
