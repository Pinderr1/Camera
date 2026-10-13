"""Camera stream addressing: brand templates, URL building, safe descriptions.

A camera `source` is either structured (preferred, what the dashboard writes):

    {"type": "rtsp", "host": "192.168.1.20", "port": 554, "template": "hikvision",
     "channel": 1, "username": "viewer", "credential": "cam_driveway",
     "prefer_substream": true, "hw_decode": "auto"}

or a legacy `url` that may contain `${ENV}` / `${secret:NAME}` placeholders.
Passwords live only in the credential store; `public_source()` is the only view
of a source that may leave the process (API responses, logs)."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import quote, urlsplit

# Common factory defaults. Many DVR/NVR brands are OEM rebrands of these
# platforms, but firmware varies: the wizard tests every candidate before saving.
BRAND_TEMPLATES: dict[str, dict[str, Any]] = {
    "hikvision": {
        "label": "Hikvision (and Hikvision-based OEMs)", "port": 554,
        "main": "/Streaming/Channels/{channel}01", "sub": "/Streaming/Channels/{channel}02",
        "snapshot": "/ISAPI/Streaming/channels/{channel}01/picture",
    },
    "dahua": {
        "label": "Dahua, Amcrest, many Lorex models", "port": 554,
        "main": "/cam/realmonitor?channel={channel}&subtype=0", "sub": "/cam/realmonitor?channel={channel}&subtype=1",
        "snapshot": "/cgi-bin/snapshot.cgi?channel={channel}",
    },
    "reolink": {
        "label": "Reolink", "port": 554,
        "main": "/h264Preview_{channel:02d}_main", "sub": "/h264Preview_{channel:02d}_sub",
    },
    "uniview": {
        "label": "Uniview", "port": 554,
        "main": "/unicast/c{channel}/s0/live", "sub": "/unicast/c{channel}/s1/live",
    },
    "tapo": {"label": "TP-Link Tapo / VIGI", "port": 554, "main": "/stream1", "sub": "/stream2"},
    "axis": {
        "label": "Axis", "port": 554,
        "main": "/axis-media/media.amp?camera={channel}", "sub": "/axis-media/media.amp?camera={channel}&resolution=640x360",
        "snapshot": "/axis-cgi/jpg/image.cgi?camera={channel}",
    },
    "custom": {"label": "Custom path", "port": 554},
}

STREAM_TYPES = {"rtsp", "http", "usb", "capture", "file", "simulated"}
_PLACEHOLDER = re.compile(r"\$\{secret:([a-z0-9][a-z0-9_.\-]*)\}")
SecretLookup = Callable[[str], "str | None"]


def has_substream(source: dict[str, Any]) -> bool:
    if source.get("sub_url") or source.get("sub_path"):
        return True
    template = BRAND_TEMPLATES.get(source.get("template", ""), {})
    return "sub" in template and not source.get("path")


def selected_stream(source: dict[str, Any], purpose: str = "ai") -> str:
    """'sub' for AI processing when the camera offers one and the source prefers it."""
    if purpose == "ai" and source.get("prefer_substream", True) and has_substream(source):
        return "sub"
    return "main"


def stream_path(source: dict[str, Any], which: str) -> str:
    if which == "sub" and source.get("sub_path"):
        return source["sub_path"]
    if source.get("path"):
        return source["path"]
    template = BRAND_TEMPLATES.get(source.get("template", ""), {})
    pattern = template.get(which) or template.get("main") or "/"
    return pattern.format(channel=int(source.get("channel", 1)))


def build_url(source: dict[str, Any], which: str = "main", secrets: SecretLookup | None = None) -> str:
    """Full stream URL with credentials, for handing to the decoder only."""
    if source.get("url"):
        url = source["sub_url"] if which == "sub" and source.get("sub_url") else source["url"]
        url = _PLACEHOLDER.sub(lambda match: quote(_secret(match.group(1), secrets), safe=""), url)
        parts = urlsplit(url)
        if source.get("credential") and parts.username and parts.password is None:
            password = quote(_secret(source["credential"], secrets), safe="")
            netloc = f"{parts.username}:{password}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
            url = parts._replace(netloc=netloc).geturl()
        return url
    scheme = "rtsp" if source["type"] == "rtsp" else "http"
    port = int(source.get("port") or (554 if scheme == "rtsp" else 80))
    userinfo = ""
    if source.get("username"):
        password = _secret(source["credential"], secrets) if source.get("credential") else ""
        userinfo = quote(source["username"], safe="") + (":" + quote(password, safe="") if password else "") + "@"
    path = stream_path(source, which)
    return f"{scheme}://{userinfo}{source['host']}:{port}{path if path.startswith('/') else '/' + path}"


def public_source(source: dict[str, Any]) -> dict[str, Any]:
    """What the API may show about a source: never URLs, paths, or passwords."""
    view: dict[str, Any] = {"type": source.get("type", "")}
    if source.get("type") in {"rtsp", "http"}:
        host = source.get("host") or (urlsplit(_PLACEHOLDER.sub("x", source["url"])).hostname if source.get("url") else "")
        view |= {
            "host": host or "",
            "port": source.get("port") or (urlsplit(_PLACEHOLDER.sub("x", source["url"])).port if source.get("url") else None),
            "template": source.get("template", "custom" if source.get("url") or source.get("path") else ""),
            "channel": source.get("channel"),
            "username": source.get("username", ""),
            "has_credentials": bool(source.get("credential") or (source.get("url") and "@" in source["url"])),
            "has_substream": has_substream(source),
            "prefer_substream": source.get("prefer_substream", True),
            "mode": source.get("mode", "stream"),
        }
    elif source.get("type") in {"usb", "capture"}:
        view |= {"index": source.get("index", 0), "backend": source.get("backend", "auto"), "resolution": source.get("resolution")}
    elif source.get("type") == "file":
        view |= {"file": str(source.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]}
    elif source.get("type") == "simulated":
        view |= {"scenario": str(source.get("scenario", "")).replace("\\", "/").rsplit("/", 1)[-1]}
    view["hw_decode"] = source.get("hw_decode", "auto")
    return view


def log_name(camera: dict[str, Any]) -> str:
    """Identifier for log lines: camera id and host, nothing else."""
    host = public_source(camera.get("source", {})).get("host")
    return f"{camera.get('id', '?')}@{host}" if host else str(camera.get("id", "?"))


def scrub(text: str) -> str:
    """Remove anything URL-like from library error text before it is logged or returned."""
    return re.sub(r"\b(rtsp|rtsps|http|https)://\S+", r"\1://[redacted]", text)


def _secret(name: str, secrets: SecretLookup | None) -> str:
    value = secrets(name) if secrets else None
    if value is None:
        raise KeyError(f"credential '{name}' is not set")
    return value
