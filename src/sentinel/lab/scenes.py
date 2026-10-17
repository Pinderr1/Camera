"""Test Lab scenes: zones and rules for one recorded clip (stdlib).

A scene is what the Lab pretends the clip's camera watches: named zones drawn on
the footage and the rules of a preset (residential, retail, parking lot,
wildlife). Scenes are saved per video in <storage>/lab/scenes/<clip>.json and
only ever build the Lab's own one-camera site; they never touch the site config
or a live camera.

Lab zone types are the engine's zone types plus `public` (a shop floor, lobby or
other place where people are expected). Public areas, sidewalks and roads are
ordinary activity: detections and info events are still logged there, but no
preset rule alerts on them except after hours. In the engine a public area is a
`general` zone, which preset rules never name."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ZONE_TYPES = {
    "entrance": "Entrance (door, gate)",
    "public": "Public area (shop floor, lobby)",
    "restricted": "Restricted area (staff only, shed)",
    "driveway": "Driveway / parking",
    "yard": "Yard",
    "sidewalk": "Sidewalk",
    "street": "Road",
    "perimeter": "Fence line",
    "ignore": "Ignore (no detection)",
}
ENGINE_TYPE = {"public": "general"}  # every other Lab type is an engine type of the same name
ORDINARY_TYPES = {"public", "sidewalk", "street"}
PROTECTED_TYPES = {"entrance", "restricted"}  # Guardian's protected zones in the Lab
MAX_ZONES = 12
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CLIP_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")


def _rect(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _restricted_entry(prefix: str, name: str = "Person entered a restricted area") -> dict[str, Any]:
    return {"id": f"{prefix}_restricted", "name": name, "type": "zone_entry", "zone_types": ["restricted"],
            "classes": ["person"], "severity": "high", "min_observations": 3}


# Rules name zone *types*, so they apply to whatever zones are drawn. Severities
# follow the alert policy: info is logged only, low pushes in Vacation, medium in
# Away/Night, high in every armed mode. Names describe behaviour, never intent.
PRESETS: dict[str, dict[str, Any]] = {
    "residential": {
        "name": "Residential", "description": "Front of a house: visits to the door are logged; alerts for restricted areas, lingering at the entrance and night-time visits.",
        "zones": [{"name": "Yard", "type": "yard", "polygon": _rect(0, 0, 1, 0.65)},
                  {"name": "Entrance", "type": "entrance", "polygon": _rect(0, 0.65, 1, 1)}],
        "rules": [
            {"id": "res_entrance_day", "name": "Person at the entrance (daytime)", "type": "zone_entry", "zone_types": ["entrance"],
             "classes": ["person"], "severity": "low", "min_observations": 3, "active_hours": {"start": "06:00", "end": "22:00"}},
            {"id": "res_entrance_night", "name": "Person at the entrance at night", "type": "zone_entry", "zone_types": ["entrance"],
             "classes": ["person"], "severity": "medium", "min_observations": 3, "active_hours": {"start": "22:00", "end": "06:00"}},
            {"id": "res_linger", "name": "Person lingering at the entrance", "type": "dwell", "zone_types": ["entrance", "perimeter"],
             "classes": ["person"], "min_dwell_s": 60, "max_roam": 3, "severity": "medium"},
            _restricted_entry("res"),
            {"id": "res_vehicle", "name": "Vehicle stopped in the driveway", "type": "dwell", "zone_types": ["driveway"],
             "classes": ["vehicle"], "min_dwell_s": 60, "require_arrival": True, "severity": "low"},
        ],
    },
    "retail": {
        "name": "Retail", "description": "A shop: customers on the shop floor are ordinary activity. Alerts for staff-only areas, lingering at the entrance and anyone in the shop after hours.",
        "zones": [{"name": "Shop floor", "type": "public", "polygon": _rect(0, 0, 1, 1)}],
        "rules": [
            _restricted_entry("ret", "Person entered a staff-only area"),
            {"id": "ret_after_hours", "name": "Person in the shop after hours", "type": "zone_entry", "zone_types": ["public", "entrance", "restricted"],
             "classes": ["person"], "severity": "high", "min_observations": 3, "active_hours": {"start": "22:00", "end": "07:00"}},
            {"id": "ret_entrance_linger", "name": "Person lingering at the entrance", "type": "dwell", "zone_types": ["entrance"],
             "classes": ["person"], "min_dwell_s": 180, "max_roam": 2, "severity": "medium"},
        ],
    },
    "parking_lot": {
        "name": "Parking lot", "description": "Cars and people coming and going are ordinary. Alerts for restricted areas, a person staying among parked cars, and activity after hours.",
        "zones": [{"name": "Road", "type": "street", "polygon": _rect(0, 0, 1, 0.15)},
                  {"name": "Parking area", "type": "driveway", "polygon": _rect(0, 0.15, 1, 1)}],
        "rules": [
            _restricted_entry("pk"),
            {"id": "pk_after_hours", "name": "Person in the parking area after hours", "type": "zone_entry", "zone_types": ["driveway"],
             "classes": ["person"], "severity": "medium", "min_observations": 3, "active_hours": {"start": "23:00", "end": "05:00"}},
            {"id": "pk_linger", "name": "Person remained among parked vehicles", "type": "dwell", "zone_types": ["driveway"],
             "classes": ["person"], "min_dwell_s": 300, "max_roam": 3, "severity": "medium"},
            {"id": "pk_vehicle_after_hours", "name": "Vehicle stopped after hours", "type": "dwell", "zone_types": ["driveway"],
             "classes": ["vehicle"], "min_dwell_s": 120, "require_arrival": True, "severity": "medium", "active_hours": {"start": "23:00", "end": "05:00"}},
        ],
    },
    "wildlife": {
        "name": "Wildlife", "description": "Animals are judged by Wilderness Guardian (movement toward the house). People only alert near the house at night or in restricted areas.",
        "zones": [{"name": "Yard", "type": "yard", "polygon": _rect(0, 0, 1, 0.7)},
                  {"name": "Near the house", "type": "entrance", "polygon": _rect(0, 0.7, 1, 1)}],
        "rules": [
            {"id": "wl_person_night", "name": "Person near the house at night", "type": "zone_entry", "zone_types": ["entrance"],
             "classes": ["person"], "severity": "medium", "min_observations": 3, "active_hours": {"start": "22:00", "end": "06:00"}},
            _restricted_entry("wl"),
        ],
    },
}


def default_preset(clip: Any) -> str:
    conditions = set(getattr(clip, "conditions", []) or [])
    if "retail" in conditions:
        return "retail"
    if "parking" in conditions:
        return "parking_lot"
    if getattr(clip, "source", "") == "guardian" or "animal" in (getattr(clip, "subjects", []) or []):
        return "wildlife"
    return "residential"


def preset_scene(preset: str) -> dict[str, Any]:
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {sorted(PRESETS)}")
    return {"preset": preset, "zones": _with_ids(copy.deepcopy(PRESETS[preset]["zones"])), "rules": {}}


def _path(storage_dir: Path, clip_id: str) -> Path:
    if not _CLIP_ID.match(clip_id):
        raise ValueError("invalid clip id")
    return Path(storage_dir) / "lab" / "scenes" / f"{clip_id}.json"


def load(storage_dir: Path, clip_id: str) -> dict[str, Any] | None:
    path = _path(storage_dir, clip_id)
    if not path.exists():
        return None
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        return None  # a damaged scene file falls back to the default; it is replaced on the next save


def save(storage_dir: Path, clip_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    scene = validate(body)
    scene.update({"updated": datetime.now().isoformat(timespec="seconds"), "actor": actor})
    path = _path(storage_dir, clip_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(scene, indent=1), encoding="utf-8")
    tmp.replace(path)
    return scene


def reset(storage_dir: Path, clip_id: str) -> None:
    _path(storage_dir, clip_id).unlink(missing_ok=True)


def validate(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("scene must be an object")
    preset = body.get("preset")
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {sorted(PRESETS)}")
    zones = body.get("zones")
    if not isinstance(zones, list) or not 1 <= len(zones) <= MAX_ZONES:
        raise ValueError(f"a scene needs 1 to {MAX_ZONES} zones")
    clean = []
    for index, zone in enumerate(zones):
        where = f"zones[{index}]"
        if not isinstance(zone, dict):
            raise ValueError(f"{where} must be an object")
        name = str(zone.get("name", "")).strip()[:40]
        if not name:
            raise ValueError(f"{where} needs a name")
        if zone.get("type") not in ZONE_TYPES:
            raise ValueError(f"{where}.type must be one of {sorted(ZONE_TYPES)}")
        polygon = zone.get("polygon")
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= 20:
            raise ValueError(f"{where}.polygon needs 3 to 20 points")
        points = []
        for point in polygon:
            if not (isinstance(point, (list, tuple)) and len(point) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in point)):
                raise ValueError(f"{where}.polygon points must be [x, y] in 0..1")
            points.append([round(float(point[0]), 4), round(float(point[1]), 4)])
        if _area(points) < 0.0005:
            raise ValueError(f"{where} ({name}) is too small")
        clean.append({"name": name, "type": zone["type"], "polygon": points})
    rule_ids = {rule["id"] for rule in PRESETS[preset]["rules"]}
    overrides: dict[str, dict[str, Any]] = {}
    for rule_id, change in (body.get("rules") or {}).items():
        if rule_id not in rule_ids or not isinstance(change, dict):
            raise ValueError(f"unknown rule {rule_id!r} for preset {preset}")
        item: dict[str, Any] = {}
        if "enabled" in change:
            item["enabled"] = bool(change["enabled"])
        if change.get("severity") is not None:
            if change["severity"] not in {"info", "low", "medium", "high"}:
                raise ValueError(f"{rule_id}: severity must be info, low, medium or high")
            item["severity"] = change["severity"]
        if change.get("min_dwell_s") is not None:
            seconds = change["min_dwell_s"]
            if not isinstance(seconds, (int, float)) or not 5 <= seconds <= 3600:
                raise ValueError(f"{rule_id}: min_dwell_s must be 5..3600")
            item["min_dwell_s"] = float(seconds)
        if change.get("active_hours") is not None:
            hours = change["active_hours"]
            if not (isinstance(hours, dict) and _TIME.match(str(hours.get("start", ""))) and _TIME.match(str(hours.get("end", "")))):
                raise ValueError(f"{rule_id}: active_hours needs start and end as HH:MM")
            if hours["start"] == hours["end"]:
                raise ValueError(f"{rule_id}: active_hours start and end must differ")
            item["active_hours"] = {"start": hours["start"], "end": hours["end"]}
        if item:
            overrides[rule_id] = item
    return {"preset": preset, "zones": _with_ids(clean), "rules": overrides}


def rules(scene: dict[str, Any]) -> list[dict[str, Any]]:
    """The preset's rules with the scene's changes, in Lab zone types."""
    out = []
    for rule in copy.deepcopy(PRESETS[scene["preset"]]["rules"]):
        change = scene.get("rules", {}).get(rule["id"], {})
        if "min_dwell_s" in change and rule["type"] != "dwell":
            change = {k: v for k, v in change.items() if k != "min_dwell_s"}
        out.append(rule | change)
    return out


def site_parts(scene: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(engine zones, engine rules, Guardian protected zone ids) for the Lab site."""
    zones = [{"id": z["id"], "name": z["name"], "type": ENGINE_TYPE.get(z["type"], z["type"]), "polygon": z["polygon"]} for z in scene["zones"]]
    engine_rules = []
    for rule in rules(scene):
        rule = dict(rule)
        if rule.get("zone_types"):
            rule["zone_types"] = sorted({ENGINE_TYPE.get(t, t) for t in rule["zone_types"]})
        engine_rules.append(rule)
    protected = [z["id"] for z in scene["zones"] if z["type"] in PROTECTED_TYPES]
    return zones, engine_rules, protected


def catalog() -> dict[str, Any]:
    return {"zone_types": [{"id": key, "name": name, "ordinary": key in ORDINARY_TYPES} for key, name in ZONE_TYPES.items()],
            "presets": [{"id": key, "name": p["name"], "description": p["description"], "zones": p["zones"], "rules": p["rules"]}
                        for key, p in PRESETS.items()]}


def _with_ids(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": f"lab_z{index + 1}"} | {k: v for k, v in zone.items() if k != "id"} for index, zone in enumerate(zones)]


def _area(points: list[list[float]]) -> float:
    return abs(sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points)))) / 2
