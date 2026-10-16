"""Validation of the `guardian` config block and per-camera `guardian` options."""

from __future__ import annotations

from typing import Any

from .geometry import GroundCalibration
from .monitor import GuardianSettings
from .movement import MovementSettings
from .risk import CONCERNS, LEVELS, RiskSettings
from .species import SpeciesSettings

_NUMBERS = ("min_confidence", "min_track_s", "sustained_s", "hold_s", "cooldown_s", "reacquire_s", "reacquire_body_heights", "closing_rate", "closing_rate_m")


def validate_guardian(config: dict[str, Any], camera_ids: set[str], zone_ids: set[str], errors: list[str]) -> None:
    guardian = config.get("guardian")
    if guardian is not None:
        if not isinstance(guardian, dict):
            errors.append("guardian must be an object")
            return
        unknown = set(guardian) - set(GuardianSettings.__dataclass_fields__)
        if unknown:
            errors.append(f"guardian has unknown keys {sorted(unknown)}")
        if not isinstance(guardian.get("enabled", False), bool):
            errors.append("guardian.enabled must be true or false")
        for key in _NUMBERS:
            value = guardian.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                errors.append(f"guardian.{key} must be a non-negative number")
        if isinstance(guardian.get("min_confidence"), (int, float)) and guardian["min_confidence"] > 1:
            errors.append("guardian.min_confidence must be between 0 and 1")
        for key in ("cameras", "protected_zones", "protected_zone_types", "ignore_kinds"):
            value = guardian.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"guardian.{key} must be a list of names")  # a bare string would match substrings
                guardian = {**guardian, key: []}
        for camera_id in guardian.get("cameras", []):
            if camera_id not in camera_ids:
                errors.append(f"guardian.cameras: unknown camera {camera_id!r}")
        for zone_id in guardian.get("protected_zones", []):
            if zone_id not in zone_ids:
                errors.append(f"guardian.protected_zones: unknown zone {zone_id!r}")
        for block, fields in (("movement", MovementSettings.__dataclass_fields__), ("risk", RiskSettings.__dataclass_fields__)):
            values = guardian.get(block, {})
            if not isinstance(values, dict):
                errors.append(f"guardian.{block} must be an object")
                continue
            unknown = set(values) - set(fields)
            if unknown:
                errors.append(f"guardian.{block} has unknown keys {sorted(unknown)}")
            for key, value in values.items():
                if key in fields and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                    errors.append(f"guardian.{block}.{key} must be a non-negative number")
        risk = guardian.get("risk", {})
        if isinstance(risk, dict):
            scores = [risk.get(key, getattr(RiskSettings(), key)) for key in ("approaching_score", "elevated_score", "urgent_score")]
            numeric = all(isinstance(s, (int, float)) and not isinstance(s, bool) for s in scores)
            if numeric and not scores[0] < scores[1] < scores[2]:
                errors.append("guardian.risk scores must rise: approaching_score < elevated_score < urgent_score")
        if not isinstance(guardian.get("species", {}), dict):
            errors.append("guardian.species must be an object")
            guardian = {**guardian, "species": {}}
        if guardian.get("notify_min_level", "elevated_concern") not in LEVELS[1:]:
            errors.append(f"guardian.notify_min_level must be one of {list(LEVELS[1:])}")
        if guardian.get("sensitivity", "normal") not in {"low", "normal", "high"}:
            errors.append("guardian.sensitivity must be low, normal or high")
        for kind, override in guardian.get("species", {}).items():
            if not isinstance(override, dict) or override.get("concern") not in CONCERNS:
                errors.append(f"guardian.species.{kind}.concern must be one of {list(CONCERNS)}")
        _validate_species_classifier(guardian.get("species_classifier", {}), errors)
    for index, camera in enumerate(config.get("cameras", [])):
        options = camera.get("guardian")
        if options is None:
            continue
        path = f"cameras[{index}].guardian"
        if not isinstance(options, dict) or set(options) - {"calibration", "moving_camera"}:
            errors.append(f"{path} accepts only calibration and moving_camera")
            continue
        calibration = options.get("calibration")
        if calibration is not None:
            try:
                image, ground = calibration["image_points"], calibration["ground_points_m"]
                if any(not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in image):
                    raise ValueError("image_points must be normalised to 0..1")
                GroundCalibration.fit(image, ground)
            except (KeyError, TypeError, IndexError, ValueError) as error:
                errors.append(f"{path}.calibration is invalid: {error}")


def _validate_species_classifier(block: Any, errors: list[str]) -> None:
    path = "guardian.species_classifier"
    if not isinstance(block, dict):
        errors.append(f"{path} must be an object")
        return
    unknown = set(block) - set(SpeciesSettings.__dataclass_fields__)
    if unknown:
        errors.append(f"{path} has unknown keys {sorted(unknown)}")
    if not isinstance(block.get("enabled", False), bool):
        errors.append(f"{path}.enabled must be true or false")
    for key in ("min_confidence", "min_margin", "group_confidence", "blank_confidence"):
        value = block.get(key, 0.5)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            errors.append(f"{path}.{key} must be between 0 and 1")
    for key in ("reclassify_s", "pad", "blank_recheck_s"):
        value = block.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            errors.append(f"{path}.{key} must be a non-negative number")
    for key in ("min_agreeing", "max_crops_per_track", "queue_size", "min_box_px", "cpu_threads"):
        value = block.get(key, 1)
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "cpu_threads" else 1):
            errors.append(f"{path}.{key} must be a positive whole number")
    country = block.get("country")
    if country is not None and not (isinstance(country, str) and len(country) == 3 and country.isalpha()):
        errors.append(f"{path}.country must be an ISO 3166-1 alpha-3 code such as CAN or USA")
    if not isinstance(block.get("model_dir", ""), str):
        errors.append(f"{path}.model_dir must be a path")
