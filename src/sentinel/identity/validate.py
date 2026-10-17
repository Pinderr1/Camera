"""Validation of the `identity` config block (called from config.validate_config). Standard library only."""

from __future__ import annotations

from typing import Any

from .settings import FORBIDDEN_ZONE_TYPES, LIMITS, IdentitySettings

KEYS = set(IdentitySettings.__dataclass_fields__)


def validate_identity(config: dict[str, Any], zone_types: dict[str, str], errors: list[str]) -> None:
    block = config.get("identity")
    if block is None:
        return
    if not isinstance(block, dict):
        errors.append("identity must be an object")
        return
    unknown = set(block) - KEYS
    if unknown:
        errors.append(f"identity has unknown keys {sorted(unknown)}")
    if "enabled" in block and not isinstance(block["enabled"], bool):
        errors.append("identity.enabled must be true or false")
    zones = block.get("zones", [])
    if not isinstance(zones, list) or any(not isinstance(zone, str) for zone in zones):
        errors.append("identity.zones must be a list of zone ids")
        zones = []
    for zone_id in zones:
        if zone_id not in zone_types:
            errors.append(f"identity.zones references unknown zone {zone_id!r}")
        elif zone_types[zone_id] in FORBIDDEN_ZONE_TYPES:
            errors.append(f"identity.zones: {zone_id!r} is a {zone_types[zone_id]} zone; faces are never recognized in public or excluded areas")
    if block.get("enabled") and not zones:
        errors.append("identity.enabled needs at least one recognition zone in identity.zones (recognition never runs camera-wide)")
    for key, (low, high) in LIMITS.items():
        if key in block:
            value = block[key]
            integer = isinstance(IdentitySettings.__dataclass_fields__[key].default, int)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)) or not low <= value <= high:
                errors.append(f"identity.{key} must be {'an integer' if integer else 'a number'} between {low:g} and {high:g}")
    match, unknown_below = block.get("match_threshold", 0.45), block.get("unknown_below", 0.30)
    if isinstance(match, (int, float)) and isinstance(unknown_below, (int, float)) and unknown_below > match:
        errors.append("identity.unknown_below must not be above identity.match_threshold")
    for key in ("detector_model", "recognizer_model"):
        if key in block and not (isinstance(block[key], str) and block[key].strip()):
            errors.append(f"identity.{key} must be a model file path")


def validate_identity_rule(rule: dict[str, Any], path: str, errors: list[str]) -> None:
    """Rule fields that use recognition: `identity_filter` and `identity_wait_s`."""
    if rule.get("identity_filter", "any") not in ("any", "not_known"):
        errors.append(f"{path}.identity_filter must be any or not_known")
    if "identity_wait_s" in rule:
        value = rule["identity_wait_s"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
            errors.append(f"{path}.identity_wait_s must be between 0 and 10 seconds")
    if rule.get("identity_filter") == "not_known" and "person" not in rule.get("classes", ["person"]):
        errors.append(f"{path}.identity_filter applies to people only; add person to classes or remove the filter")
