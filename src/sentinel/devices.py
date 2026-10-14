"""Unified device registry: cameras, recorders, drones, hubs, sensors and docks.

Cameras keep their own `cameras[]` section (the pipeline depends on it); every
other device lives in `devices[]`. This module describes what each kind of
device can do (capabilities), how well Sentinel supports a piece of hardware
(compatibility profiles with a verification status), validates the `devices`
section, and builds one list of devices with their live state for the API.

Registration is never treated as proof of anything. A device is `registered`
when it is in the configuration; `connected` only when Sentinel receives data
from it (frames, telemetry, sensor reports); video and AI verification are
separate levels on top. Hardware without a driver stays `not_connected`.

Standard library only, so it can be unit-tested anywhere."""

from __future__ import annotations

import re
from typing import Any

DEVICE_CATEGORIES: dict[str, str] = {
    "camera": "Fixed camera",
    "recorder": "DVR / NVR recorder",
    "drone": "Drone",
    "hub": "AI hub",
    "contact_sensor": "Door / window sensor",
    "motion_sensor": "Motion sensor",
    "environmental_sensor": "Environmental sensor",
    "dock": "Charging dock",
}
# Cameras are registered through the camera onboarding flow (cameras[]), and the
# hub is this computer; neither is stored in devices[].
REGISTRABLE = ("recorder", "drone", "contact_sensor", "motion_sensor", "environmental_sensor", "dock")
SENSOR_CATEGORIES = ("contact_sensor", "motion_sensor", "environmental_sensor")

CAPABILITIES: dict[str, str] = {
    "video_stream": "Video streaming",
    "image_capture": "Image capture",
    "motion_sensing": "Motion sensing",
    "contact_sensing": "Door/window contact",
    "environment_sensing": "Temperature, smoke or water sensing",
    "telemetry": "Telemetry",
    "battery": "Battery monitoring",
    "external_power": "External power",
    "network": "Network connectivity",
    "recording": "Recording",
    "inspection": "Inspection support",
    "health_monitoring": "Health monitoring",
    "charging": "Charging",
    "ai_processing": "AI processing",
}

# How far Sentinel's support for a piece of hardware has been proven.
SUPPORT_LEVELS: dict[str, str] = {
    "verified": "Verified on physical hardware",
    "simulated": "Works with Sentinel's built-in simulator (no hardware)",
    "expected": "Should work through a standard protocol Sentinel implements; not yet tested on this hardware",
    "limited": "Partly supported; read the notes",
    "planned": "Planned; not available in this build",
    "unsupported": "Not supported",
}

# Live connection levels, from weakest to strongest. Each one is earned by data
# Sentinel actually received, never by registration alone.
LEVELS = ("registered", "connected", "video", "ai_verified")
LEVEL_LABELS = {
    "registered": "Registered",
    "connected": "Connected",
    "video": "Video available",
    "ai_verified": "AI monitoring verified",
}

# Hardware compatibility profiles. No prices, and no model specifications that
# have not been checked: power and codec entries name the standard or the
# typical option, and notes say what has (and has not) been tested.
PROFILES: list[dict[str, Any]] = [
    {
        "id": "onvif_ip_camera", "category": "camera", "name": "ONVIF Profile S IP camera (any brand)", "support": "expected",
        "protocols": ["RTSP", "ONVIF"], "codecs": ["H.264 (recommended)", "H.265 (needs hardware decoding to be affordable)"],
        "power": ["PoE (802.3af)", "12 V DC adapter"], "capabilities": ["video_stream", "image_capture", "network", "external_power"],
        "template": "custom",
        "notes": ["Discovery, profile reading and RTSP tests are verified against a simulated ONVIF camera only.",
                  "Use the camera's sub-stream for AI (640x360 to 640x480, 5-10 fps, H.264)."],
    },
    {
        "id": "hikvision_camera", "category": "camera", "name": "Hikvision IP camera", "support": "expected",
        "protocols": ["RTSP", "ONVIF (when enabled)", "SADP discovery"], "codecs": ["H.264", "H.265 / H.264+ (turn off for the AI stream)"],
        "power": ["PoE (802.3af)", "12 V DC adapter"], "capabilities": ["video_stream", "image_capture", "network", "external_power"],
        "template": "hikvision",
        "notes": ["A new camera must be activated (admin password set) before use.", "Not yet tested on a physical Hikvision camera."],
    },
    {
        "id": "hikvision_recorder", "category": "recorder", "name": "Hikvision DVR / NVR (including ERI-K108-P8)", "support": "expected",
        "protocols": ["RTSP per channel", "SADP discovery", "HTTP"], "codecs": ["H.264", "H.265"],
        "power": ["Mains adapter"], "capabilities": ["video_stream", "recording", "network", "external_power"],
        "template": "hikvision",
        "notes": ["Channel n streams are /Streaming/Channels/n01 (main) and n02 (sub).",
                  "Our first install (ERI-K108-P8, eight cameras) is not yet connected: the recorder sits on another address range (ROADMAP Phase 0.8).",
                  "Several wrong passwords lock the account for about 30 minutes."],
    },
    {
        "id": "dahua_family", "category": "recorder", "name": "Dahua, Amcrest and Dahua-based Lorex recorders", "support": "expected",
        "protocols": ["RTSP per channel", "ONVIF (when enabled)", "HTTP"], "codecs": ["H.264", "H.265"],
        "power": ["Mains adapter"], "capabilities": ["video_stream", "recording", "network", "external_power"],
        "template": "dahua",
        "notes": ["Many Lorex recorders run Dahua firmware; some Lorex models limit or disable RTSP.", "Not yet tested on physical hardware."],
    },
    {
        "id": "dahua_camera", "category": "camera", "name": "Dahua, Amcrest or Lorex IP camera", "support": "expected",
        "protocols": ["RTSP", "ONVIF (when enabled)"], "codecs": ["H.264", "H.265"],
        "power": ["PoE (802.3af)", "12 V DC adapter"], "capabilities": ["video_stream", "image_capture", "network", "external_power"],
        "template": "dahua", "notes": ["Not yet tested on physical hardware."],
    },
    {
        "id": "reolink", "category": "camera", "name": "Reolink camera or NVR", "support": "limited",
        "protocols": ["RTSP (when enabled)", "ONVIF (when enabled)"], "codecs": ["H.264", "H.265"],
        "power": ["PoE (802.3af)", "12 V DC adapter", "Battery (no continuous stream)"],
        "capabilities": ["video_stream", "image_capture", "network"], "template": "reolink",
        "notes": ["Recent firmware ships with RTSP and ONVIF turned off: turn them on in the Reolink app first.",
                  "Battery models sleep between events and do not provide the continuous stream Sentinel needs.",
                  "Not yet tested on physical hardware."],
    },
    {
        "id": "uniview", "category": "camera", "name": "Uniview IP camera or NVR", "support": "expected",
        "protocols": ["RTSP", "ONVIF"], "codecs": ["H.264", "H.265"], "power": ["PoE (802.3af)", "12 V DC adapter"],
        "capabilities": ["video_stream", "image_capture", "network"], "template": "uniview", "notes": ["Not yet tested on physical hardware."],
    },
    {
        "id": "generic_recorder", "category": "recorder", "name": "Other DVR / NVR with RTSP or ONVIF", "support": "expected",
        "protocols": ["RTSP", "ONVIF", "HTTP snapshot (fallback)"], "codecs": ["H.264", "H.265"], "power": ["Mains adapter"],
        "capabilities": ["video_stream", "recording", "network", "external_power"], "template": "custom",
        "notes": ["Check the manual for the RTSP path. If the recorder offers no RTSP, ONVIF or HTTP snapshot, use its HDMI output with a capture card."],
    },
    {
        "id": "http_snapshot", "category": "camera", "name": "Camera or recorder with HTTP snapshot / MJPEG only", "support": "expected",
        "protocols": ["HTTP snapshot", "MJPEG"], "codecs": ["JPEG"], "power": ["Varies"],
        "capabilities": ["image_capture", "network"], "notes": ["Polled snapshots: lower frame rate than a video stream. Not yet tested on hardware."],
    },
    {
        "id": "usb_capture", "category": "camera", "name": "USB webcam or analog/HDMI capture device", "support": "expected",
        "protocols": ["USB video (UVC)"], "codecs": ["Raw / MJPEG over USB"], "power": ["USB"],
        "capabilities": ["video_stream", "image_capture"],
        "notes": ["The practical route for analog cameras whose DVR exposes no usable stream. USB and capture discovery are not yet tested on hardware."],
    },
    {
        "id": "cloud_only_camera", "category": "camera", "name": "Cloud-only camera (no RTSP, ONVIF or HTTP stream)", "support": "unsupported",
        "protocols": ["Vendor cloud app only"], "codecs": [], "power": ["Varies"], "capabilities": [],
        "notes": ["Sentinel processes video locally and needs a local stream. Many battery doorbells and subscription cameras only stream to the vendor's cloud.",
                  "Check the model's manual for RTSP or ONVIF before buying; Sentinel does not claim compatibility otherwise."],
    },
    {
        "id": "simulated_camera", "category": "camera", "name": "Simulated camera (demo and testing)", "support": "simulated",
        "protocols": ["Built-in"], "codecs": [], "power": [], "capabilities": ["video_stream", "image_capture"], "notes": ["Scripted scenes; no hardware."],
    },
    {
        "id": "sentinel_camera", "category": "camera", "name": "Sentinel camera", "support": "planned",
        "protocols": ["RTSP", "ONVIF"], "codecs": ["H.264"], "power": ["PoE"], "capabilities": ["video_stream", "image_capture", "network"],
        "notes": ["Future Sentinel-branded camera. No specifications have been decided."],
    },
    {
        "id": "simulated_drone", "category": "drone", "name": "Simulated drone", "support": "simulated", "adapter": "simulated",
        "protocols": ["Built-in simulator"], "codecs": [], "power": ["Battery (simulated)"],
        "capabilities": ["telemetry", "battery", "inspection", "health_monitoring", "charging"],
        "notes": ["Kinematic simulator: approvals, geofence and safety checks are real; the aircraft is not."],
    },
    {
        "id": "mavlink_drone", "category": "drone", "name": "ArduPilot / PX4 drone over MAVLink", "support": "limited", "adapter": "mavlink",
        "protocols": ["MAVLink"], "codecs": [], "power": ["Battery"], "capabilities": ["telemetry", "battery", "health_monitoring"],
        "notes": ["Telemetry only: every flight command is refused in this build (docs/drone-integration.md).",
                  "Not yet tested against ArduPilot SITL or a physical aircraft."],
    },
    {
        "id": "sentinel_drone_outdoor", "category": "drone", "name": "Sentinel outdoor drone", "support": "planned",
        "protocols": ["MAVLink"], "codecs": ["H.264"], "power": ["Battery"], "capabilities": ["video_stream", "telemetry", "battery", "inspection"],
        "notes": ["Future product; hardware is being evaluated separately. Register a simulated drone to set up the rest of the system now."],
    },
    {
        "id": "sentinel_drone_indoor", "category": "drone", "name": "Sentinel indoor drone", "support": "planned",
        "protocols": ["MAVLink"], "codecs": ["H.264"], "power": ["Battery"], "capabilities": ["video_stream", "telemetry", "battery", "inspection"],
        "notes": ["Future product. Indoor flight needs positioning without GPS, which Sentinel's geofence does not yet model."],
    },
    {
        "id": "simulated_dock", "category": "dock", "name": "Simulated charging dock", "support": "simulated",
        "protocols": ["Built-in"], "codecs": [], "power": ["Simulated"], "capabilities": ["charging", "external_power", "health_monitoring"],
        "notes": ["Reports docked and charging state of the simulated drone."],
    },
    {
        "id": "sentinel_dock", "category": "dock", "name": "Sentinel drone dock", "support": "planned",
        "protocols": [], "codecs": [], "power": ["Mains"], "capabilities": ["charging", "external_power"],
        "notes": ["Future product. Commercial docks expose state through vendor SDKs and will need their own adapter."],
    },
    {
        "id": "simulated_contact", "category": "contact_sensor", "name": "Simulated door/window sensor", "support": "simulated",
        "protocols": ["Built-in"], "codecs": [], "power": ["Battery (simulated)"], "capabilities": ["contact_sensing", "battery", "health_monitoring"],
        "notes": ["Reports battery and check-ins; a test button records an information event. Sensors do not open incidents yet."],
    },
    {
        "id": "simulated_motion", "category": "motion_sensor", "name": "Simulated motion sensor", "support": "simulated",
        "protocols": ["Built-in"], "codecs": [], "power": ["Battery (simulated)"], "capabilities": ["motion_sensing", "battery", "health_monitoring"],
        "notes": ["Reports battery and check-ins; a test button records an information event. Sensors do not open incidents yet."],
    },
    {
        "id": "simulated_environmental", "category": "environmental_sensor", "name": "Simulated environmental sensor", "support": "simulated",
        "protocols": ["Built-in"], "codecs": [], "power": ["Battery (simulated)"], "capabilities": ["environment_sensing", "battery", "health_monitoring"],
        "notes": ["Reports battery and check-ins only."],
    },
    {
        "id": "mqtt_sensor", "category": "contact_sensor", "name": "Zigbee / Z-Wave sensor via MQTT or Home Assistant", "support": "planned",
        "protocols": ["MQTT"], "codecs": [], "power": ["Battery"], "capabilities": ["contact_sensing", "battery"],
        "notes": ["Sensor inputs to rules are on the roadmap (Phase 4). It can be registered for planning, but Sentinel will not receive its data yet."],
    },
    {
        "id": "mqtt_motion_sensor", "category": "motion_sensor", "name": "Motion sensor via MQTT or Home Assistant", "support": "planned",
        "protocols": ["MQTT"], "codecs": [], "power": ["Battery"], "capabilities": ["motion_sensing", "battery"],
        "notes": ["Planned (Phase 4). Registration only; no data is received yet."],
    },
    {
        "id": "mqtt_environmental", "category": "environmental_sensor", "name": "Smoke, water or temperature sensor via MQTT", "support": "planned",
        "protocols": ["MQTT"], "codecs": [], "power": ["Battery"], "capabilities": ["environment_sensing", "battery"],
        "notes": ["Planned. Sentinel is not a certified fire or flood alarm and must not replace one."],
    },
    {
        "id": "this_computer", "category": "hub", "name": "This computer running Sentinel", "support": "verified",
        "protocols": ["Local network"], "codecs": [], "power": ["Mains"],
        "capabilities": ["ai_processing", "recording", "health_monitoring", "network", "external_power"],
        "notes": ["Sentinel's software hub. Detection performance depends on its CPU/GPU (ROADMAP benchmark findings)."],
    },
    {
        "id": "sentinel_hub", "category": "hub", "name": "Sentinel Hub (dedicated hardware)", "support": "planned",
        "protocols": ["Local network"], "codecs": [], "power": ["Mains"], "capabilities": ["ai_processing", "recording", "health_monitoring", "network"],
        "notes": ["Future product. Today the hub is the computer that runs Sentinel."],
    },
]
PROFILE_INDEX = {profile["id"]: profile for profile in PROFILES}
DRONE_ADAPTERS = ("simulated", "mavlink")
_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_PASSWORD_IN_URL = re.compile(r"://[^/@\s]+:[^/@\s$]+@")


def catalog() -> dict[str, Any]:
    return {
        "categories": DEVICE_CATEGORIES,
        "registrable": list(REGISTRABLE),
        "capabilities": CAPABILITIES,
        "support_levels": SUPPORT_LEVELS,
        "levels": LEVEL_LABELS,
        "profiles": PROFILES,
    }


def profiles_for(category: str) -> list[dict[str, Any]]:
    return [profile for profile in PROFILES if profile["category"] == category]


def default_profile(category: str, simulated: bool) -> str:
    candidates = profiles_for(category)
    preferred = [p for p in candidates if (p["support"] == "simulated") == simulated]
    return (preferred or candidates or [{"id": ""}])[0]["id"]


def camera_profile(source: dict[str, Any]) -> str:
    """Best matching compatibility profile for a configured camera source."""
    kind = source.get("type")
    if kind == "simulated":
        return "simulated_camera"
    if kind in ("usb", "capture"):
        return "usb_capture"
    if kind == "http":
        return "http_snapshot"
    if kind == "file":
        return ""
    return {"hikvision": "hikvision_camera", "dahua": "dahua_camera", "reolink": "reolink", "uniview": "uniview"}.get(
        source.get("template", ""), "onvif_ip_camera")


def validate_devices(config: dict[str, Any], camera_ids: set[str], zone_ids: set[str], errors: list[str]) -> None:
    """Checks `devices[]`, camera `recorder` references, and `drone.device`."""
    devices = config.get("devices", [])
    if not isinstance(devices, list):
        errors.append("devices must be a list")
        return
    seen: set[str] = set()
    recorders: set[str] = set()
    drones: set[str] = set()
    for index, device in enumerate(devices):
        path = f"devices[{index}]"
        if not isinstance(device, dict):
            errors.append(f"{path} must be an object")
            continue
        device_id = device.get("id", "")
        if not isinstance(device_id, str) or not _ID.match(device_id):
            errors.append(f"{path}.id must be a lowercase identifier")
        elif device_id in seen or device_id in camera_ids:
            errors.append(f"{path}.id duplicates {device_id!r}; device and camera ids are site-wide")
        seen.add(device_id)
        category = device.get("category")
        if category not in REGISTRABLE:
            errors.append(f"{path}.category must be one of {list(REGISTRABLE)}")
            continue
        if category == "recorder":
            recorders.add(device_id)
        if category == "drone":
            drones.add(device_id)
        name = device.get("name", "")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= 60:
            errors.append(f"{path}.name must be 1-60 characters")
        profile = PROFILE_INDEX.get(device.get("profile", ""))
        if profile is None or profile["category"] != category:
            errors.append(f"{path}.profile must be a {category} profile from the hardware catalog")
        elif profile["support"] == "unsupported":
            errors.append(f"{path}.profile {profile['id']} is not supported")
        if "model" in device and not (isinstance(device["model"], str) and len(device["model"]) <= 60):
            errors.append(f"{path}.model must be at most 60 characters")
        if "simulated" in device and not isinstance(device["simulated"], bool):
            errors.append(f"{path}.simulated must be true or false")
        if profile and device.get("simulated") and profile["support"] != "simulated":
            errors.append(f"{path}: only simulator profiles can be simulated")
        position = device.get("map")
        if position is not None and not (isinstance(position, dict) and all(_is_number(position.get(key)) for key in ("x", "y"))):
            errors.append(f"{path}.map needs numeric x and y (metres)")
        zone = device.get("zone_id")
        if zone and zone not in zone_ids:
            errors.append(f"{path}.zone_id references unknown zone {zone!r}")
        if category == "recorder":
            host = device.get("host", "")
            if host and (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host)):
                errors.append(f"{path}.host must be an address or host name (no credentials)")
            for key in ("http_port", "rtsp_port", "channels"):
                if key in device and not (isinstance(device[key], int) and 0 < device[key] <= (64 if key == "channels" else 65535)):
                    errors.append(f"{path}.{key} is out of range")
        if category == "drone":
            adapter = device.get("adapter", (profile or {}).get("adapter", "simulated"))
            if adapter not in DRONE_ADAPTERS:
                errors.append(f"{path}.adapter must be one of {list(DRONE_ADAPTERS)}")
            if profile and profile.get("adapter") and adapter != profile["adapter"]:
                errors.append(f"{path}.adapter must be {profile['adapter']} for profile {profile['id']}")
            connection = device.get("connection", "")
            if adapter == "mavlink" and not connection:
                errors.append(f"{path}.connection is required for a MAVLink drone (e.g. udp:127.0.0.1:14550)")
            if connection and _PASSWORD_IN_URL.search(str(connection)):
                errors.append(f"{path}.connection must not embed a password")
            if "indoor" in device and not isinstance(device["indoor"], bool):
                errors.append(f"{path}.indoor must be true or false")
        if category == "dock" and device.get("drone_id") and device["drone_id"] not in {d.get("id") for d in devices}:
            errors.append(f"{path}.drone_id references unknown device {device['drone_id']!r}")
    for index, camera in enumerate(config.get("cameras") or []):
        recorder = camera.get("recorder")
        if recorder and recorder not in recorders:
            errors.append(f"cameras[{index}].recorder references unknown recorder {recorder!r}")
    drone = config.get("drone", {})
    if drone.get("device") and drone["device"] not in drones:
        errors.append(f"drone.device references unknown drone {drone['device']!r}")
    for index, point in enumerate(drone.get("inspection_points", [])):
        path = f"drone.inspection_points[{index}]"
        if not (isinstance(point, dict) and isinstance(point.get("id"), str) and _ID.match(point["id"])):
            errors.append(f"{path}.id must be a lowercase identifier")
        elif not (isinstance(point.get("map"), list) and len(point["map"]) == 2 and all(_is_number(v) for v in point["map"])):
            errors.append(f"{path}.map must be [x, y] in metres")


def drone_devices(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Registered drones Sentinel can actually talk to: simulated ones and
    MAVLink (telemetry-only) ones. Planned hardware gets no adapter at all, so
    it can never appear connected or ready."""
    operable = []
    for device in config.get("devices", []):
        if device.get("category") != "drone" or not device.get("enabled", True):
            continue
        adapter = device.get("adapter") or PROFILE_INDEX.get(device.get("profile", ""), {}).get("adapter")
        if (adapter == "simulated" and device.get("simulated")) or adapter == "mavlink":
            operable.append(device)
    return operable


def has_registered_drones(config: dict[str, Any]) -> bool:
    return any(device.get("category") == "drone" for device in config.get("devices", []))


def primary_drone_id(config: dict[str, Any]) -> str:
    drones = drone_devices(config)
    wanted = config.get("drone", {}).get("device")
    if wanted and any(device["id"] == wanted for device in drones):
        return wanted
    return drones[0]["id"] if drones else ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---- live view -----------------------------------------------------------------------


def _level(connected: bool, video: bool, ai: bool) -> str:
    return "ai_verified" if ai else "video" if video else "connected" if connected else "registered"


def camera_entry(camera: dict[str, Any], status: dict[str, Any] | None, now_ms: int, *, running: bool) -> dict[str, Any]:
    from_status = status or {}
    health = from_status.get("health", {})
    last_frame = int(from_status.get("last_frame_ms") or 0)
    fresh = bool(last_frame) and now_ms - last_frame < 15_000
    connected = bool(from_status.get("connected")) or fresh
    video = fresh
    ai = health.get("state") == "monitoring" and health.get("monitoring") is True
    source = camera.get("source", {})
    profile = camera_profile(source)
    if not running:
        state, detail = "not_running", "Camera processing is not running in this session"
    elif not camera.get("enabled", True):
        state, detail = "disabled", "Disabled"
    else:
        state = health.get("state", "starting")
        detail = health.get("label") or state
        if state == "unverified":
            detail = "Picture is still and cannot be confirmed as live"
    return {
        "id": camera["id"], "category": "camera", "name": camera.get("name", camera["id"]), "profile": profile,
        "support": PROFILE_INDEX.get(profile, {}).get("support", "expected"), "simulated": source.get("type") == "simulated",
        "capabilities": PROFILE_INDEX.get(profile, {}).get("capabilities", ["video_stream"]),
        "source_type": source.get("type", ""), "recorder": camera.get("recorder", ""),
        "level": _level(connected and running, video and running, ai and running), "state": state, "detail": detail,
        "map": camera.get("map"), "zones": len(camera.get("zones", [])), "privacy_masks": len(camera.get("privacy_masks", [])),
        "warnings": list(health.get("warnings", [])), "removable": True,
    }


def device_entry(device: dict[str, Any], live: dict[str, Any] | None, cameras_by_recorder: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """`live` is whatever the service knows about the device right now (drone
    status, sensor report); None means Sentinel has no driver for it."""
    profile = PROFILE_INDEX.get(device.get("profile", ""), {})
    category = device["category"]
    entry = {
        "id": device["id"], "category": category, "name": device.get("name", device["id"]), "profile": profile.get("id", ""),
        "support": profile.get("support", "planned"), "simulated": bool(device.get("simulated")),
        "capabilities": profile.get("capabilities", []), "map": device.get("map"), "zone_id": device.get("zone_id", ""),
        "warnings": [], "removable": True,
    }
    if category == "recorder":
        channels = cameras_by_recorder.get(device["id"], [])
        reachable = [camera for camera in channels if camera["level"] != "registered"]
        entry |= {"host": device.get("host", ""), "brand_template": profile.get("template", "custom"), "channels_added": len(channels),
                  "channels_expected": device.get("channels")}
        entry["level"] = max((camera["level"] for camera in channels), key=LEVELS.index, default="registered")
        if not channels:
            entry |= {"state": "no_channels", "detail": "No channels added yet: find the recorder's channels and add them as cameras"}
        elif reachable:
            entry |= {"state": "connected", "detail": f"{len(reachable)} of {len(channels)} channel(s) delivering video"}
        else:
            entry |= {"state": "offline", "detail": "No channel is delivering video"}
        return entry
    if live is None:
        reason = ("Planned hardware: registration only, Sentinel cannot connect to it yet" if profile.get("support") == "planned"
                  else "Not connected")
        return entry | {"level": "registered", "state": "not_connected", "detail": reason}
    if category == "drone":
        telemetry = live.get("telemetry", {})
        connected = bool(telemetry.get("connected") and telemetry.get("link_ok"))
        blockers = live.get("preflight_blockers", [])
        entry |= {
            "level": "connected" if connected else "registered",
            "state": "ready" if connected and not blockers else "connected" if connected else "link_lost" if telemetry.get("connected") else "not_connected",
            "detail": ("Ready for operator-approved inspections" if connected and not blockers
                       else f"Not ready: {', '.join(b.replace('_', ' ') for b in blockers)}" if connected else "No telemetry link"),
            "battery_pct": telemetry.get("battery_pct"), "docked": telemetry.get("docked"), "charging": telemetry.get("charging"),
            "flight_mode": telemetry.get("flight_mode"), "safety_state": live.get("safety_state"), "primary": live.get("primary", False),
            "physical": live.get("physical", False), "video": "Drone video is not ingested in this build",
        }
        if not live.get("primary"):
            entry["warnings"].append("Only the primary drone accepts inspection requests in this build")
        return entry
    if category == "dock":
        drone = live.get("drone_telemetry") or {}
        entry |= {"level": "connected" if drone else "registered", "state": "connected" if drone else "not_connected",
                  "detail": ("Drone docked" + (", charging" if drone.get("charging") else "")) if drone.get("docked")
                  else "Drone away from dock" if drone else "No drone assigned"}
        return entry
    # sensors
    connected = live.get("reporting", False)
    entry |= {"level": "connected" if connected else "registered", "state": "reporting" if connected else "silent",
              "detail": f"Last report {live.get('last_report_age_s', '?')} s ago" if connected else "No reports received",
              "battery_pct": live.get("battery_pct"), "last_event": live.get("last_event")}
    if live.get("battery_pct") is not None and live["battery_pct"] < 20:
        entry["warnings"].append("Battery low")
    return entry


class SimulatedSensors:
    """Check-ins and battery for simulated sensors, so the registry, readiness
    checks and the dashboard can be exercised without hardware. A test press
    is reported to the caller, which logs it as an information event; sensors
    never open incidents in this build."""

    REPORT_EVERY_S = 30.0
    DRAIN_PCT_PER_REPORT = 0.002

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}

    def sync(self, config: dict[str, Any], now_ms: int) -> None:
        wanted = {device["id"] for device in config.get("devices", [])
                  if device.get("category") in SENSOR_CATEGORIES and device.get("simulated") and device.get("enabled", True)}
        for device_id in list(self.state):
            if device_id not in wanted:
                del self.state[device_id]
        for device_id in wanted:
            self.state.setdefault(device_id, {"battery_pct": 100.0, "last_report_ms": now_ms, "last_event": None})

    def step(self, now_ms: int) -> None:
        for sensor in self.state.values():
            if now_ms - sensor["last_report_ms"] >= self.REPORT_EVERY_S * 1000:
                sensor["last_report_ms"] = now_ms
                sensor["battery_pct"] = max(0.0, sensor["battery_pct"] - self.DRAIN_PCT_PER_REPORT)

    def trigger(self, device_id: str, now_ms: int) -> dict[str, Any]:
        sensor = self.state[device_id]
        sensor["last_report_ms"] = now_ms
        sensor["last_event"] = {"kind": "test", "ms": now_ms}
        return dict(sensor)

    def live(self, device_id: str, now_ms: int) -> dict[str, Any] | None:
        sensor = self.state.get(device_id)
        if sensor is None:
            return None
        age_s = round((now_ms - sensor["last_report_ms"]) / 1000)
        return {"reporting": age_s <= 3 * self.REPORT_EVERY_S, "last_report_age_s": age_s, "battery_pct": round(sensor["battery_pct"], 1),
                "last_event": sensor["last_event"]}


def hub_entry(resources: dict[str, Any] | None) -> dict[str, Any]:
    resources = resources or {}
    gpu = resources.get("gpu") or {}
    return {
        "id": "hub_local", "category": "hub", "name": "This computer (Sentinel hub)", "profile": "this_computer", "support": "verified",
        "simulated": False, "capabilities": PROFILE_INDEX["this_computer"]["capabilities"], "level": "connected", "state": "running",
        "detail": "Running Sentinel" + (f" · GPU {gpu.get('name')}" if gpu.get("name") else ""),
        "cpu_pct": resources.get("system_cpu_pct"), "warnings": [], "removable": False, "map": None,
    }
