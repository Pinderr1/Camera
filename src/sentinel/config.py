from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
from typing import Any

from senior_safety.config_validation import ConfigValidationError, read_json_object

from .models import SEVERITIES

ZONE_TYPES = {"driveway", "entrance", "yard", "perimeter", "restricted", "street", "ignore", "general"}
SOURCE_TYPES = {"rtsp", "http", "usb", "file", "simulated"}
RULE_TYPES = {"zone_entry", "dwell", "repeated_presence", "sequence", "multi_camera"}
ARM_MODES = ("disarmed", "home", "away")
SENSITIVITIES = {"low": (1.4, 0.10), "normal": (1.0, 0.0), "high": (0.7, -0.08)}
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


def load_config(path: str | Path, *, resolve_secrets: bool = True) -> dict[str, Any]:
    config = read_json_object(path, "sentinel")
    validate_config(config)
    config["_config_dir"] = str(Path(path).resolve().parent)
    if resolve_secrets:
        config = resolve_env(config)
    return config


def resolve_env(value: Any) -> Any:
    """Substitute ${VAR} references so camera passwords and tokens live in the
    environment (or a .env loaded by the service manager), never in config files."""
    if isinstance(value, dict):
        if value.get("enabled") is False:
            return value  # disabled channels/cameras may reference variables that are not set
        return {key: resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env(item) for item in value]
    if isinstance(value, str) and "${" in value:
        missing = [name for name in _ENV_PATTERN.findall(value) if name not in os.environ]
        if missing:
            raise ConfigValidationError([f"environment variable(s) not set: {', '.join(missing)}"])
        return _ENV_PATTERN.sub(lambda match: os.environ[match.group(1)], value)
    return value


def redact_url(url: str) -> str:
    return re.sub(r"(?<=://)[^/@\s]+@", "***@", url)


def public_camera_view(camera: dict[str, Any]) -> dict[str, Any]:
    """Camera config safe to return over the API: no source URLs or credentials."""
    view = {key: copy.deepcopy(value) for key, value in camera.items() if key not in {"source"}}
    source = camera.get("source", {})
    view["source_type"] = source.get("type", "")
    return view


def cameras(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [camera for camera in config.get("cameras", []) if camera.get("enabled", True)]


def zone_index(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for camera in config.get("cameras", []):
        for zone in camera.get("zones", []):
            index[zone["id"]] = zone | {"camera_id": camera["id"]}
    return index


def sensitivity_adjustments(sensitivity: str) -> tuple[float, float]:
    """(dwell-time multiplier, detector score-threshold offset)."""
    return SENSITIVITIES.get(sensitivity, SENSITIVITIES["normal"])


def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []
    site = config.get("site")
    if not isinstance(site, dict) or not site.get("id"):
        errors.append("site.id is required")

    camera_ids: set[str] = set()
    zone_ids: set[str] = set()
    raw_cameras = config.get("cameras")
    if not isinstance(raw_cameras, list) or not raw_cameras:
        errors.append("cameras must be a non-empty list")
        raw_cameras = []
    for index, camera in enumerate(raw_cameras):
        path = f"cameras[{index}]"
        camera_id = camera.get("id", "")
        _identifier(camera_id, f"{path}.id", errors)
        if camera_id in camera_ids:
            errors.append(f"{path}.id duplicates {camera_id!r}")
        camera_ids.add(camera_id)
        source = camera.get("source")
        if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
            errors.append(f"{path}.source.type must be one of {sorted(SOURCE_TYPES)}")
        elif source["type"] in {"rtsp", "http"}:
            url = str(source.get("url", ""))
            if re.search(r"://[^/@\s]+:[^/@\s$]+@", url) and "${" not in url:
                errors.append(f"{path}.source.url embeds a literal password; use ${{ENV_VAR}} substitution")
        fps = camera.get("fps", 5)
        if not _is_number(fps) or not 0 < fps <= 30:
            errors.append(f"{path}.fps must be in (0, 30]")
        for mask_index, mask in enumerate(camera.get("privacy_masks", [])):
            _polygon(mask, f"{path}.privacy_masks[{mask_index}]", errors)
        for zone_index_, zone in enumerate(camera.get("zones", [])):
            zone_path = f"{path}.zones[{zone_index_}]"
            zone_id = zone.get("id", "")
            _identifier(zone_id, f"{zone_path}.id", errors)
            if zone_id in zone_ids:
                errors.append(f"{zone_path}.id duplicates {zone_id!r}; zone ids are site-wide")
            zone_ids.add(zone_id)
            if zone.get("type") not in ZONE_TYPES:
                errors.append(f"{zone_path}.type must be one of {sorted(ZONE_TYPES)}")
            _polygon(zone.get("polygon"), f"{zone_path}.polygon", errors)
            if "map_polygon" in zone:
                _polygon(zone["map_polygon"], f"{zone_path}.map_polygon", errors, normalized=False)

    for index, link in enumerate(config.get("topology", {}).get("links", [])):
        path = f"topology.links[{index}]"
        for key in ("from", "to"):
            if link.get(key) not in camera_ids:
                errors.append(f"{path}.{key} references unknown camera {link.get(key)!r}")
        low, high = link.get("min_transit_s", 0), link.get("max_transit_s")
        if not _is_number(high) or not _is_number(low) or high <= low or high > 600:
            errors.append(f"{path} needs min_transit_s < max_transit_s <= 600")
        for key in ("from_zones", "to_zones"):
            for zone_id in link.get(key, []):
                if zone_id not in zone_ids:
                    errors.append(f"{path}.{key} references unknown zone {zone_id!r}")

    rule_ids: set[str] = set()
    for index, rule in enumerate(config.get("rules", [])):
        path = f"rules[{index}]"
        _identifier(rule.get("id", ""), f"{path}.id", errors)
        if rule.get("id") in rule_ids:
            errors.append(f"{path}.id duplicates {rule.get('id')!r}")
        rule_ids.add(rule.get("id"))
        rule_type = rule.get("type")
        if rule_type not in RULE_TYPES:
            errors.append(f"{path}.type must be one of {sorted(RULE_TYPES)}")
            continue
        if rule.get("severity") not in SEVERITIES:
            errors.append(f"{path}.severity must be one of {list(SEVERITIES)}")
        for zone_id in rule.get("zones", []):
            if zone_id not in zone_ids:
                errors.append(f"{path}.zones references unknown zone {zone_id!r}")
        for zone_type in rule.get("zone_types", []):
            if zone_type not in ZONE_TYPES:
                errors.append(f"{path}.zone_types has unknown type {zone_type!r}")
        for mode in rule.get("arm_modes", []):
            if mode not in ARM_MODES:
                errors.append(f"{path}.arm_modes has unknown mode {mode!r}")
        if rule_type == "dwell" and not _positive(rule.get("min_dwell_s")):
            errors.append(f"{path}.min_dwell_s must be positive")
        if "max_roam" in rule and not _positive(rule["max_roam"]):
            errors.append(f"{path}.max_roam must be positive (body heights)")
        if rule_type == "repeated_presence":
            if not isinstance(rule.get("count"), int) or rule["count"] < 2:
                errors.append(f"{path}.count must be an integer >= 2")
            if not _positive(rule.get("window_s")):
                errors.append(f"{path}.window_s must be positive")
        if rule_type == "sequence":
            if len(rule.get("zones", [])) < 2:
                errors.append(f"{path}.zones needs at least two zones in order")
            if not _positive(rule.get("within_s")):
                errors.append(f"{path}.within_s must be positive")
        if rule_type == "multi_camera":
            if not isinstance(rule.get("min_cameras"), int) or rule["min_cameras"] < 2:
                errors.append(f"{path}.min_cameras must be an integer >= 2")
            if not _positive(rule.get("window_s")):
                errors.append(f"{path}.window_s must be positive")
        active_hours = rule.get("active_hours")
        if active_hours is not None and not (
            isinstance(active_hours, dict)
            and all(re.fullmatch(r"\d{2}:\d{2}", str(active_hours.get(key, ""))) for key in ("start", "end"))
        ):
            errors.append(f"{path}.active_hours needs start/end as HH:MM")

    alerts = config.get("alerts", {})
    for mode, minimum in alerts.get("notify_min_severity", {}).items():
        if mode not in ARM_MODES or (minimum != "none" and minimum not in SEVERITIES):
            errors.append(f"alerts.notify_min_severity.{mode} is invalid")

    defaults = config.get("settings_defaults", {})
    if defaults.get("arm_mode", "home") not in ARM_MODES:
        errors.append("settings_defaults.arm_mode is invalid")
    if defaults.get("sensitivity", "normal") not in SENSITIVITIES:
        errors.append("settings_defaults.sensitivity is invalid")

    if errors:
        raise ConfigValidationError(errors)


def _identifier(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        errors.append(f"{path} must be a lowercase identifier")


def _polygon(points: Any, path: str, errors: list[str], *, normalized: bool = True) -> None:
    if not isinstance(points, list) or len(points) < 3:
        errors.append(f"{path} needs at least 3 points")
        return
    for point in points:
        if not (isinstance(point, list) and len(point) == 2 and all(_is_number(v) for v in point)):
            errors.append(f"{path} has a malformed point {point!r}")
            return
        if normalized and not all(0.0 <= v <= 1.0 for v in point):
            errors.append(f"{path} point {point!r} must be normalized to 0..1")
            return


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> bool:
    return _is_number(value) and value > 0
