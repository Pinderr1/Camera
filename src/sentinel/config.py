from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
from typing import Any

from senior_safety.config_validation import ConfigValidationError, read_json_object

from .models import SEVERITIES
from .streams import BRAND_TEMPLATES

ZONE_TYPES = {"driveway", "entrance", "yard", "sidewalk", "perimeter", "restricted", "street", "ignore", "general"}
SOURCE_TYPES = {"rtsp", "http", "usb", "capture", "file", "simulated"}
CATEGORIES = ("person", "vehicle", "bicycle", "animal", "bag", "unknown_motion")
RULE_TYPES = {"zone_entry", "dwell", "repeated_presence", "sequence", "multi_camera", "combination"}
ARM_MODES = ("disarmed", "home", "away", "night", "vacation")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NOTIFY_PREFERENCES = ("auto", "always", "never")
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
    """Camera config safe to return over the API: no stream URLs, paths, or credentials."""
    from .streams import public_source

    view = {key: copy.deepcopy(value) for key, value in camera.items() if key not in {"source"}}
    source = camera.get("source", {})
    view["source_type"] = source.get("type", "")
    view["source"] = public_source(source)
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
    raw_cameras = config.get("cameras", [])
    if not isinstance(raw_cameras, list):
        errors.append("cameras must be a list (it may be empty: a site can start with drones or sensors)")
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
            for key in ("url", "sub_url"):
                if re.search(r"://[^/@\s]+:[^/@\s$]+@", str(source.get(key, ""))) and "${" not in str(source.get(key, "")):
                    errors.append(f"{path}.source.{key} embeds a literal password; use the credential store or ${{ENV_VAR}}")
            if not url and not source.get("host"):
                errors.append(f"{path}.source needs a host (or a url)")
            if source.get("template") and source["template"] not in BRAND_TEMPLATES:
                errors.append(f"{path}.source.template must be one of {sorted(BRAND_TEMPLATES)}")
            if source.get("mode", "stream") not in {"stream", "snapshot"}:
                errors.append(f"{path}.source.mode must be stream or snapshot")
        elif source["type"] in {"usb", "capture"} and not isinstance(source.get("index", 0), int):
            errors.append(f"{path}.source.index must be an integer device index")
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
        if rule_type == "sequence" and rule.get("enabled", True):
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
        _validate_rule_extras(rule, path, rule_type, errors)

    rules_by_id = {rule.get("id"): rule for rule in config.get("rules", [])}
    for index, rule in enumerate(config.get("rules", [])):
        if rule.get("type") != "combination":
            continue
        path = f"rules[{index}]"
        components = rule.get("rules")
        if not isinstance(components, list) or not components:
            errors.append(f"{path}.rules must list the rules that have to match")
            continue
        for component in components:
            if component == rule.get("id"):
                errors.append(f"{path}.rules cannot include the rule itself")
            elif component not in rules_by_id:
                errors.append(f"{path}.rules references unknown rule {component!r}")
            elif rules_by_id[component].get("type") == "combination":
                errors.append(f"{path}.rules cannot include another combination rule ({component})")
        if not isinstance(rule.get("min_matches"), int) or not 1 <= rule["min_matches"] <= len(set(components)):
            errors.append(f"{path}.min_matches must be between 1 and the number of listed rules")
        if not _positive(rule.get("within_s")):
            errors.append(f"{path}.within_s must be positive")
        if rule.get("scope", "subject") not in ("subject", "site"):
            errors.append(f"{path}.scope must be subject (same linked subject) or site (anywhere on the property)")

    for scope, categories in [("categories", config.get("categories", {}))] + [
        (f"cameras[{index}].categories", camera.get("categories", {})) for index, camera in enumerate(raw_cameras)
    ]:
        for name, settings in categories.items():
            if name not in CATEGORIES:
                errors.append(f"{scope}.{name} is not a known category {list(CATEGORIES)}")
            elif not isinstance(settings, dict) or any(key not in {"detect", "alerts"} or not isinstance(value, bool) for key, value in settings.items()):
                errors.append(f"{scope}.{name} needs boolean detect/alerts")

    alerts = config.get("alerts", {})
    for mode, minimum in alerts.get("notify_min_severity", {}).items():
        if mode not in ARM_MODES or (minimum != "none" and minimum not in SEVERITIES):
            errors.append(f"alerts.notify_min_severity.{mode} is invalid")

    _validate_reliability(config, raw_cameras, errors)

    from .devices import validate_devices

    validate_devices(config, camera_ids, zone_ids, errors)
    from .assistant.settings import validate_assistant

    validate_assistant(config, errors)
    _validate_drone_and_channels(config, errors)

    defaults = config.get("settings_defaults", {})
    if defaults.get("arm_mode", "home") not in ARM_MODES:
        errors.append("settings_defaults.arm_mode is invalid")
    if defaults.get("sensitivity", "normal") not in SENSITIVITIES:
        errors.append("settings_defaults.sensitivity is invalid")

    if errors:
        raise ConfigValidationError(errors)


def _validate_rule_extras(rule: dict[str, Any], path: str, rule_type: str, errors: list[str]) -> None:
    """Rule-builder fields: name, description, days, cooldown, notification
    preference, minimum observations, and category classes."""
    if "name" in rule and (not isinstance(rule["name"], str) or not 0 < len(rule["name"].strip()) <= 80):
        errors.append(f"{path}.name must be 1-80 characters")
    if "description" in rule and (not isinstance(rule["description"], str) or len(rule["description"]) > 300):
        errors.append(f"{path}.description must be at most 300 characters")
    days = rule.get("active_days")
    if days is not None and (not isinstance(days, list) or not days or any(day not in DAYS for day in days)):
        errors.append(f"{path}.active_days must be a non-empty list of {list(DAYS)}")
    if "cooldown_s" in rule and not (_is_number(rule["cooldown_s"]) and 0 <= rule["cooldown_s"] <= 86400):
        errors.append(f"{path}.cooldown_s must be between 0 and 86400")
    if rule.get("notify", "auto") not in NOTIFY_PREFERENCES:
        errors.append(f"{path}.notify must be one of {list(NOTIFY_PREFERENCES)}")
    if "min_observations" in rule and not (isinstance(rule["min_observations"], int) and 1 <= rule["min_observations"] <= 100):
        errors.append(f"{path}.min_observations must be an integer from 1 to 100")
    if "min_confidence" in rule and not (_is_number(rule["min_confidence"]) and 0 <= rule["min_confidence"] <= 1):
        errors.append(f"{path}.min_confidence must be between 0 and 1")
    classes = rule.get("classes")
    if classes is not None and (not isinstance(classes, list) or not classes or any(name not in CATEGORIES for name in classes)):
        errors.append(f"{path}.classes must be a non-empty list of {list(CATEGORIES)}")
    hours = rule.get("active_hours")
    if isinstance(hours, dict):
        for key in ("start", "end"):
            match = re.fullmatch(r"(\d{2}):(\d{2})", str(hours.get(key, "")))
            if match and (int(match.group(1)) > 23 or int(match.group(2)) > 59):
                errors.append(f"{path}.active_hours.{key} is not a valid time")
        if hours.get("start") == hours.get("end") and hours.get("start"):
            errors.append(f"{path}.active_hours start and end must differ")
    if rule_type == "combination" and rule.get("zones"):
        errors.append(f"{path}: a combination rule takes its zones from the rules it combines")


def _validate_drone_and_channels(config: dict[str, Any], errors: list[str]) -> None:
    drone = config.get("drone", {})
    if drone.get("enabled"):
        if not config.get("site", {}).get("map", {}).get("origin_geo"):
            errors.append("drone.enabled needs site.map.origin_geo (the property location) for the geofence")
        fence = drone.get("geofence")
        if not isinstance(fence, dict) or not (fence.get("map_polygon") or fence.get("polygon")):
            errors.append("drone.enabled needs a geofence with a map_polygon")
        else:
            key = "map_polygon" if fence.get("map_polygon") else "polygon"
            _polygon(fence[key], f"drone.geofence.{key}", errors, normalized=False)
            low, high = fence.get("min_altitude_m", 0), fence.get("max_altitude_m", 120)
            if not (_is_number(low) and _is_number(high) and 0 <= low < high <= 120):
                errors.append("drone.geofence needs 0 <= min_altitude_m < max_altitude_m <= 120")
        home = drone.get("home_map")
        if home is not None and not (isinstance(home, list) and len(home) == 2 and all(_is_number(v) for v in home)):
            errors.append("drone.home_map must be [x, y] in metres")
    for index, channel in enumerate(config.get("alerts", {}).get("channels", [])):
        path = f"alerts.channels[{index}]"
        if not isinstance(channel, dict) or channel.get("type") not in ("ntfy", "mqtt"):
            errors.append(f"{path}.type must be ntfy or mqtt")
            continue
        if channel["type"] == "ntfy" and channel.get("enabled", True):
            if not (channel.get("topic") or channel.get("topic_credential")):
                errors.append(f"{path} needs a topic (or topic_credential)")
            server = str(channel.get("server", "https://ntfy.sh"))
            if not re.match(r"^(https?://[^\s/@]+(/[^\s@]*)?|\$\{[A-Z0-9_]+\})$", server):
                errors.append(f"{path}.server must be an http(s) address without credentials")


_HEALTH_NUMBERS = ("frame_timeout_s", "frozen_after_s", "startup_grace_s", "notify_after_s", "slow_after_s", "camera_offline_after_s", "inference_timeout_s")
_SUPERVISOR_NUMBERS = ("stall_after_s", "startup_timeout_s", "backoff_initial_s", "backoff_max_s", "window_s", "cooldown_s")
_STORAGE_NUMBERS = ("min_free_mb", "min_free_pct", "resume_margin", "check_interval_s")


def _validate_reliability(config: dict[str, Any], raw_cameras: list, errors: list[str]) -> None:
    """`health`, `storage`, `logging`, detector failure handling, retention, and simulated faults."""
    health = config.get("health", {})
    if not isinstance(health, dict):
        errors.append("health must be an object")
        health = {}
    for key in _HEALTH_NUMBERS:
        if key in health and not _positive(health[key]):
            errors.append(f"health.{key} must be a positive number of seconds")
    if "unverified_notify_after_s" in health and not (_is_number(health["unverified_notify_after_s"]) and health["unverified_notify_after_s"] >= 0):
        errors.append("health.unverified_notify_after_s must be >= 0 (0 = never notify)")
    if "slow_ratio" in health and not (_is_number(health["slow_ratio"]) and 0 < health["slow_ratio"] <= 1):
        errors.append("health.slow_ratio must be in (0, 1]")
    for key in ("notifications", "notify_recovery"):
        if key in health and not isinstance(health[key], bool):
            errors.append(f"health.{key} must be true or false")
    if "max_notifications_per_hour" in health and not (isinstance(health["max_notifications_per_hour"], int) and health["max_notifications_per_hour"] >= 1):
        errors.append("health.max_notifications_per_hour must be an integer >= 1")
    for section, numbers in (("supervisor", _SUPERVISOR_NUMBERS), ("process", ("backoff_initial_s", "backoff_max_s", "window_s", "cooldown_s"))):
        values = health.get(section, {})
        for key in numbers:
            if key in values and not _positive(values[key]):
                errors.append(f"health.{section}.{key} must be a positive number of seconds")
        for key in ("max_restarts", "max_stuck_threads"):
            if key in values and not (isinstance(values[key], int) and values[key] >= 1):
                errors.append(f"health.{section}.{key} must be an integer >= 1")
    heartbeat = health.get("heartbeat", {})
    for key in ("url", "fail_url"):
        if heartbeat.get(key) and not re.match(r"^(https?://|\$\{)", str(heartbeat[key])):
            errors.append(f"health.heartbeat.{key} must be an http(s) URL or ${{ENV_VAR}}")
    if "interval_s" in heartbeat and not (_is_number(heartbeat["interval_s"]) and heartbeat["interval_s"] >= 30):
        errors.append("health.heartbeat.interval_s must be at least 30")

    storage = config.get("storage", {})
    for key in _STORAGE_NUMBERS:
        if key in storage and not _positive(storage[key]):
            errors.append(f"storage.{key} must be positive")
    if storage.get("max_media_gb") is not None and not _positive(storage["max_media_gb"]):
        errors.append("storage.max_media_gb must be positive (or omitted)")
    backups = storage.get("backups", {})
    if "interval_h" in backups and not _positive(backups["interval_h"]):
        errors.append("storage.backups.interval_h must be positive")
    if "keep" in backups and not (isinstance(backups["keep"], int) and backups["keep"] >= 1):
        errors.append("storage.backups.keep must be an integer >= 1")
    for key in ("enabled", "include_credentials"):
        if key in backups and not isinstance(backups[key], bool):
            errors.append(f"storage.backups.{key} must be true or false")

    activity = config.get("activity", {})
    for key in ("retention_days", "window_days", "recent_hours"):
        if key in activity and not _positive(activity[key]):
            errors.append(f"activity.{key} must be positive")
    for key in ("min_days", "min_samples"):
        if key in activity and not (isinstance(activity[key], int) and activity[key] >= 1):
            errors.append(f"activity.{key} must be an integer >= 1")
    if "rarity" in activity and not (_is_number(activity["rarity"]) and 0 < activity["rarity"] < 1):
        errors.append("activity.rarity must be between 0 and 1")
    if "enabled" in activity and not isinstance(activity["enabled"], bool):
        errors.append("activity.enabled must be true or false")

    logging_settings = config.get("logging", {})
    if "max_mb" in logging_settings and not _positive(logging_settings["max_mb"]):
        errors.append("logging.max_mb must be positive")
    if "backups" in logging_settings and not (isinstance(logging_settings["backups"], int) and logging_settings["backups"] >= 1):
        errors.append("logging.backups must be an integer >= 1")
    if str(logging_settings.get("level", "INFO")).upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        errors.append("logging.level must be DEBUG, INFO, WARNING or ERROR")

    retention = config.get("privacy", {}).get("retention_days", {})
    for key in ("events", "media", "protected"):
        if key in retention and not _positive(retention[key]):
            errors.append(f"privacy.retention_days.{key} must be positive")

    for scope, detector in [("detector", config.get("detector", {}))] + [
        (f"cameras[{index}].detector", camera.get("detector", {})) for index, camera in enumerate(raw_cameras)
    ]:
        if "cpu_fallback" in detector and not isinstance(detector["cpu_fallback"], bool):
            errors.append(f"{scope}.cpu_fallback must be true or false")
        if "failures_before_fallback" in detector and not (isinstance(detector["failures_before_fallback"], int) and detector["failures_before_fallback"] >= 1):
            errors.append(f"{scope}.failures_before_fallback must be an integer >= 1")
        if "retry_s" in detector and not _positive(detector["retry_s"]):
            errors.append(f"{scope}.retry_s must be positive")

    from .simulation import FAULT_TYPES

    for index, camera in enumerate(raw_cameras):
        noise = (camera.get("source") or {}).get("noise")
        if noise is not None and not (isinstance(noise, int) and 0 <= noise <= 50):
            errors.append(f"cameras[{index}].source.noise must be an integer from 0 to 50")
        faults = (camera.get("source") or {}).get("faults")
        if faults is None:
            continue
        if (camera.get("source") or {}).get("type") != "simulated":
            errors.append(f"cameras[{index}].source.faults is only allowed on simulated cameras")
            continue
        for fault_index, fault in enumerate(faults if isinstance(faults, list) else [None]):
            path = f"cameras[{index}].source.faults[{fault_index}]"
            if not isinstance(fault, dict) or fault.get("type") not in FAULT_TYPES:
                errors.append(f"{path}.type must be one of {sorted(FAULT_TYPES)}")
            elif not (_is_number(fault.get("at_s")) and fault["at_s"] >= 0) or ("duration_s" in fault and not _positive(fault["duration_s"])):
                errors.append(f"{path} needs at_s >= 0 and a positive duration_s")


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
