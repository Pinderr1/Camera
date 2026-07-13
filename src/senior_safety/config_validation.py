from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SENSOR_EVENT_NAMES = {
    "bathroom_occupied",
    "bed_occupied",
    "door_open",
    "fall_suspected",
    "floor_level_posture",
    "manual_cancel_pressed",
    "manual_help_pressed",
    "no_motion",
    "person_present",
    "route_motion",
    "sensor_offline",
    "sensor_online",
}
LEGACY_ZONE_EVENT_KEYS = {
    "fall_suppression_min_points": "fall_suppression_min_fraction",
    "motion_score_threshold": "motion_score_threshold_bl",
    "rapid_drop_min_vy": "rapid_drop_min_vy_bl",
}


class ConfigValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Invalid configuration:\n- " + "\n- ".join(errors))


def read_json_object(path: str | Path, label: str) -> dict[str, Any]:
    target = Path(path)
    try:
        with target.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as error:
        raise ConfigValidationError([f"{label}: file not found: {target}"]) from error
    except json.JSONDecodeError as error:
        raise ConfigValidationError([f"{label}: invalid JSON at line {error.lineno}, column {error.colno}"]) from error
    if not isinstance(value, dict):
        raise ConfigValidationError([f"{label}: top-level value must be an object"])
    return value


def load_sensors_config(path: str | Path) -> dict[str, Any]:
    config = read_json_object(path, "sensors")
    validate_sensors(config)
    return config


def validate_rules(rules: dict[str, Any]) -> None:
    errors: list[str] = []
    if rules.get("purpose") != "assistive_safety_monitor_not_medical_device":
        errors.append("purpose: must remain assistive_safety_monitor_not_medical_device")
    runtime = _object(rules, "runtime", errors)
    night = _object(rules, "night_window", errors)
    privacy = _object(rules, "privacy", errors)
    thresholds = _object(rules, "thresholds", errors)
    ladder = _object(rules, "alert_ladder", errors)
    suppression = _object(rules, "suppression", errors)

    _positive(runtime, "tick_interval_s", errors, "runtime")
    for key in ("start_local", "end_local"):
        value = night.get(key)
        if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
            errors.append(f"night_window.{key}: must be HH:MM in 24-hour local time")
    if night.get("start_local") == night.get("end_local"):
        errors.append("night_window: start_local and end_local must differ")
    for key, required in (
        ("process_locally", True),
        ("record_audio", False),
        ("bathroom_video_allowed", False),
    ):
        if privacy.get(key) is not required:
            errors.append(f"privacy.{key}: must be {str(required).lower()} for version 1")
    _positive(privacy, "default_media_retention_days", errors, "privacy")

    possible = _object(thresholds, "possible_fall", errors, "thresholds")
    no_return = _object(thresholds, "bed_exit_no_return", errors, "thresholds")
    bathroom = _object(thresholds, "bathroom_overstay", errors, "thresholds")
    sensor_health = _object(thresholds, "sensor_health", errors, "thresholds")
    _bounded(possible, "min_full_body_pose_confidence", 0, 1, errors, "thresholds.possible_fall")
    for key in (
        "floor_level_posture_min_s",
        "watch_before_soft_check_s",
        "urgent_after_no_motion_s",
    ):
        _positive(possible, key, errors, "thresholds.possible_fall")
    for key in ("low_notice_after_s", "urgent_after_s"):
        _positive(no_return, key, errors, "thresholds.bed_exit_no_return")
    _ordered(no_return, "low_notice_after_s", "urgent_after_s", errors, "thresholds.bed_exit_no_return")
    for key in ("low_notice_default_s", "urgent_default_s"):
        _positive(bathroom, key, errors, "thresholds.bathroom_overstay")
    _ordered(bathroom, "low_notice_default_s", "urgent_default_s", errors, "thresholds.bathroom_overstay")
    for key in ("critical_sensor_offline_after_s", "noncritical_sensor_offline_after_s"):
        _positive(sensor_health, key, errors, "thresholds.sensor_health")
    _ordered(
        sensor_health,
        "critical_sensor_offline_after_s",
        "noncritical_sensor_offline_after_s",
        errors,
        "thresholds.sensor_health",
        allow_equal=True,
    )

    for key in ("primary_ack_timeout_s", "backup_ack_timeout_s"):
        _positive(ladder, key, errors, "alert_ladder")
    if ladder.get("automatic_emergency_calls_enabled") is not False:
        errors.append("alert_ladder.automatic_emergency_calls_enabled: must be false for version 1")
    if not isinstance(ladder.get("soft_check_enabled"), bool):
        errors.append("alert_ladder.soft_check_enabled: must be true or false")
    if suppression.get("single_frame_alerts_allowed") is not False:
        errors.append("suppression.single_frame_alerts_allowed: must be false for version 1")
    _positive(suppression, "realert_cooldown_s", errors, "suppression", allow_zero=True)
    _positive(suppression, "cooldown_after_ack_s", errors, "suppression", allow_zero=True)

    rule_zone_ids = _validate_zone_descriptors(rules.get("zones"), errors, "zones", polygons_required=False)
    suppressed = suppression.get("fall_suppressed_zone_ids")
    if not isinstance(suppressed, list) or not all(isinstance(value, str) for value in suppressed):
        errors.append("suppression.fall_suppressed_zone_ids: must be a list of zone IDs")
    else:
        unknown = sorted(set(suppressed) - rule_zone_ids)
        if unknown:
            errors.append(f"suppression.fall_suppressed_zone_ids: unknown zones: {', '.join(unknown)}")
    _raise(errors)


def validate_zones(config: dict[str, Any], *, require_camera_zones: bool = False) -> None:
    errors: list[str] = []
    _identifier(config.get("camera_id"), "camera_id", errors)
    if not isinstance(config.get("room"), str) or not config["room"].strip():
        errors.append("room: must be a non-empty string")
    source = config.get("source")
    if source is not None and not (
        (isinstance(source, int) and not isinstance(source, bool) and source >= 0)
        or (isinstance(source, str) and bool(source.strip()))
    ):
        errors.append("source: must be a non-negative camera index or non-empty stream URL/path")
    if require_camera_zones and source is None:
        errors.append("source: required for a live camera configuration")
    _positive(config, "fps_limit", errors, "config")
    _bounded(config, "floor_line_y", 0, 1, errors, "config")
    zone_ids = _validate_zone_descriptors(config.get("zones"), errors, "zones", polygons_required=True)
    if require_camera_zones:
        kinds = {zone.get("kind") for zone in config.get("zones", []) if isinstance(zone, dict)}
        if not zone_ids:
            errors.append("zones: at least one camera zone is required for live monitoring")
        if "bed" not in kinds:
            errors.append("zones: a bed zone is required for live monitoring")
        if "path" not in kinds:
            errors.append("zones: a path zone is required for live monitoring")

    pose = _object(config, "pose", errors)
    if not isinstance(pose.get("model_path"), str) or not pose["model_path"].strip():
        errors.append("pose.model_path: must be a non-empty path")
    max_poses = pose.get("max_poses")
    if not isinstance(max_poses, int) or isinstance(max_poses, bool) or max_poses < 1:
        errors.append("pose.max_poses: must be a positive integer")
    for key in ("min_detection_confidence", "min_presence_confidence", "min_tracking_confidence"):
        _bounded(pose, key, 0, 1, errors, "pose")
    events = _object(config, "events", errors)
    for legacy, replacement in LEGACY_ZONE_EVENT_KEYS.items():
        if legacy in events:
            errors.append(f"events.{legacy}: obsolete key; use events.{replacement}")
    for key, value in events.items():
        if key == "motion_confirm_frames" or key.endswith(
            ("_s", "_bl", "_deg", "_ratio", "_fraction", "_threshold", "_confidence")
        ):
            if not _is_finite_number(value) or float(value) < 0:
                errors.append(f"events.{key}: must be a finite non-negative number")
        if key.endswith(("_confidence", "_fraction", "_ratio")) and (
            not _is_finite_number(value) or not 0 <= float(value) <= 1
        ):
            errors.append(f"events.{key}: must be from 0 to 1")
    suppressed = events.get("fall_suppressed_zone_ids", [])
    if not isinstance(suppressed, list) or not all(isinstance(value, str) for value in suppressed):
        errors.append("events.fall_suppressed_zone_ids: must be a list of zone IDs")
    elif set(suppressed) - zone_ids:
        errors.append(
            "events.fall_suppressed_zone_ids: unknown zones: "
            + ", ".join(sorted(set(suppressed) - zone_ids))
        )
    mqtt = config.get("mqtt")
    if mqtt is not None:
        if not isinstance(mqtt, dict):
            errors.append("mqtt: must be an object")
        else:
            host = mqtt.get("host", "localhost")
            if not isinstance(host, str) or not host.strip():
                errors.append("mqtt.host: must be a non-empty hostname or address")
            port = mqtt.get("port", 1883)
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                errors.append("mqtt.port: must be an integer from 1 to 65535")
            prefix = mqtt.get("topic_prefix", "senior-night")
            if (
                not isinstance(prefix, str)
                or not prefix.strip()
                or "+" in prefix
                or "#" in prefix
                or prefix.startswith("/")
                or prefix.endswith("/")
            ):
                errors.append("mqtt.topic_prefix: must be non-empty, relative, and cannot contain MQTT wildcards")
    _raise(errors)


def validate_sensors(config: dict[str, Any], *, require_household_minimums: bool = False) -> None:
    errors: list[str] = []
    sensors = config.get("sensors")
    if not isinstance(sensors, list):
        raise ConfigValidationError(["sensors: must be a list"])
    if require_household_minimums and not sensors:
        errors.append("sensors: at least one sensor is required for deployment")
    sensor_ids: set[str] = set()
    entity_ids: set[str] = set()
    event_names: set[str] = set()
    for index, sensor in enumerate(sensors):
        prefix = f"sensors[{index}]"
        if not isinstance(sensor, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        sensor_id = sensor.get("sensor_id")
        _identifier(sensor_id, f"{prefix}.sensor_id", errors)
        if isinstance(sensor_id, str):
            if sensor_id in sensor_ids:
                errors.append(f"{prefix}.sensor_id: duplicate ID {sensor_id!r}")
            sensor_ids.add(sensor_id)
        entity_id = sensor.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            errors.append(f"{prefix}.entity_id: must be a Home Assistant entity ID")
        elif entity_id in entity_ids:
            errors.append(f"{prefix}.entity_id: duplicate entity ID {entity_id!r}")
        else:
            entity_ids.add(entity_id)
        for key in ("sensor_type", "room", "zone_id"):
            if not isinstance(sensor.get(key), str) or not sensor[key].strip():
                errors.append(f"{prefix}.{key}: must be a non-empty string")
        event_name = sensor.get("event_name")
        if event_name not in SENSOR_EVENT_NAMES:
            errors.append(f"{prefix}.event_name: unsupported event name {event_name!r}")
        elif isinstance(event_name, str):
            event_names.add(event_name)
        if not isinstance(sensor.get("critical"), bool):
            errors.append(f"{prefix}.critical: must be true or false")

    virtual = config.get("virtual_zone_ids", [])
    if not isinstance(virtual, list) or not all(isinstance(value, str) and ID_PATTERN.fullmatch(value) for value in virtual):
        errors.append("virtual_zone_ids: must be a list of valid zone IDs")
    if require_household_minimums:
        for event_name in ("bed_occupied", "bathroom_occupied", "manual_help_pressed"):
            if event_name not in event_names:
                errors.append(f"sensors: deployment requires an {event_name!r} sensor")
        if not event_names.intersection({"person_present", "route_motion"}):
            errors.append("sensors: deployment requires a route presence or route motion sensor")
    _raise(errors)


def validate_deployment_bundle(
    rules: dict[str, Any],
    sensors: dict[str, Any],
    zones: dict[str, Any],
    *,
    paths: list[str | Path] | None = None,
) -> set[str]:
    validate_rules(rules)
    validate_sensors(sensors, require_household_minimums=True)
    validate_zones(zones, require_camera_zones=True)
    errors: list[str] = []
    for label, config in (("rules", rules), ("sensors", sensors), ("zones", zones)):
        if config.get("deployment_ready") is not True:
            errors.append(f"{label}.deployment_ready: must be explicitly set to true after household review")
    for path in paths or []:
        if ".example." in Path(path).name:
            errors.append(f"deployment config cannot use example file: {path}")
    known_zones = {
        zone["id"]
        for source in (rules.get("zones", []), zones.get("zones", []))
        for zone in source
        if isinstance(zone, dict) and isinstance(zone.get("id"), str)
    }
    known_zones.update(sensors.get("virtual_zone_ids", []))
    for sensor in sensors.get("sensors", []):
        zone_id = sensor.get("zone_id")
        if zone_id not in known_zones:
            errors.append(f"sensor {sensor.get('sensor_id')!r}: unknown zone_id {zone_id!r}")
    camera_id = zones.get("camera_id")
    pose_sensor_id = f"pose_{camera_id}"
    configured_ids = {sensor["sensor_id"] for sensor in sensors.get("sensors", [])}
    if pose_sensor_id not in configured_ids:
        errors.append(f"sensors: missing camera heartbeat sensor_id {pose_sensor_id!r}")
    else:
        pose_sensor = next(sensor for sensor in sensors["sensors"] if sensor["sensor_id"] == pose_sensor_id)
        if not pose_sensor["critical"] or pose_sensor["event_name"] != "sensor_online":
            errors.append(f"sensor {pose_sensor_id!r}: must be critical and emit sensor_online")
    _raise(errors)
    return {sensor["sensor_id"] for sensor in sensors["sensors"] if sensor["critical"]}


def reject_example_paths(paths: list[str | Path]) -> None:
    errors = [f"live runtime cannot use example file: {path}" for path in paths if ".example." in Path(path).name]
    _raise(errors)


def _validate_zone_descriptors(
    value: Any,
    errors: list[str],
    path: str,
    *,
    polygons_required: bool,
) -> set[str]:
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return set()
    zone_ids: set[str] = set()
    for index, zone in enumerate(value):
        prefix = f"{path}[{index}]"
        if not isinstance(zone, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        zone_id = zone.get("id")
        _identifier(zone_id, f"{prefix}.id", errors)
        if isinstance(zone_id, str):
            if zone_id in zone_ids:
                errors.append(f"{prefix}.id: duplicate zone ID {zone_id!r}")
            zone_ids.add(zone_id)
        if not isinstance(zone.get("kind"), str) or not zone["kind"].strip():
            errors.append(f"{prefix}.kind: must be a non-empty string")
        zone_type = zone.get("zone_type")
        if zone_type is not None and zone_type not in {"safe_rest", "fall_risk", "neutral"}:
            errors.append(f"{prefix}.zone_type: must be safe_rest, fall_risk, or neutral")
        polygon = zone.get("polygon")
        if polygon is None and not polygons_required:
            continue
        if not isinstance(polygon, list) or len(polygon) < 3:
            errors.append(f"{prefix}.polygon: must contain at least three [x, y] points")
            continue
        valid_points = True
        for point_index, point in enumerate(polygon):
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(_is_finite_number(coordinate) and 0 <= float(coordinate) <= 1 for coordinate in point)
            ):
                errors.append(f"{prefix}.polygon[{point_index}]: coordinates must be finite values from 0 to 1")
                valid_points = False
        if valid_points and abs(_polygon_area(polygon)) < 1e-6:
            errors.append(f"{prefix}.polygon: area must be greater than zero")
    return zone_ids


def _object(parent: dict[str, Any], key: str, errors: list[str], parent_path: str = "") -> dict[str, Any]:
    path = f"{parent_path}.{key}" if parent_path else key
    value = parent.get(key)
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return {}
    return value


def _identifier(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        errors.append(f"{path}: must match {ID_PATTERN.pattern}")


def _positive(
    parent: dict[str, Any],
    key: str,
    errors: list[str],
    parent_path: str,
    *,
    allow_zero: bool = False,
) -> None:
    value = parent.get(key)
    minimum_ok = _is_finite_number(value) and (float(value) >= 0 if allow_zero else float(value) > 0)
    if not minimum_ok:
        qualifier = "non-negative" if allow_zero else "positive"
        errors.append(f"{parent_path}.{key}: must be a finite {qualifier} number")


def _bounded(
    parent: dict[str, Any],
    key: str,
    lower: float,
    upper: float,
    errors: list[str],
    parent_path: str,
) -> None:
    value = parent.get(key)
    if not _is_finite_number(value) or not lower <= float(value) <= upper:
        errors.append(f"{parent_path}.{key}: must be a finite number from {lower} to {upper}")


def _ordered(
    parent: dict[str, Any],
    lower_key: str,
    upper_key: str,
    errors: list[str],
    parent_path: str,
    *,
    allow_equal: bool = False,
) -> None:
    lower = parent.get(lower_key)
    upper = parent.get(upper_key)
    if not _is_finite_number(lower) or not _is_finite_number(upper):
        return
    valid = float(lower) <= float(upper) if allow_equal else float(lower) < float(upper)
    if not valid:
        operator = "<=" if allow_equal else "<"
        errors.append(f"{parent_path}: {lower_key} must be {operator} {upper_key}")


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _polygon_area(points: list[list[float]]) -> float:
    return sum(
        float(points[index][0]) * float(points[(index + 1) % len(points)][1])
        - float(points[(index + 1) % len(points)][0]) * float(points[index][1])
        for index in range(len(points))
    ) / 2


def _raise(errors: list[str]) -> None:
    if errors:
        raise ConfigValidationError(errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate senior night safety configuration files.")
    parser.add_argument("--rules", required=True)
    parser.add_argument("--sensors", required=True)
    parser.add_argument("--zones", required=True)
    parser.add_argument("--deployment", action="store_true", help="Require live household safety minimums.")
    args = parser.parse_args()
    try:
        rules = read_json_object(args.rules, "rules")
        sensors = read_json_object(args.sensors, "sensors")
        zones = read_json_object(args.zones, "zones")
        if args.deployment:
            critical = validate_deployment_bundle(
                rules,
                sensors,
                zones,
                paths=[args.rules, args.sensors, args.zones],
            )
            print(f"Configuration valid for deployment; {len(critical)} critical sensors: {', '.join(sorted(critical))}")
        else:
            validate_rules(rules)
            validate_sensors(sensors)
            validate_zones(zones)
            print("Configuration structure valid. Use --deployment for household cross-checks.")
    except ConfigValidationError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
