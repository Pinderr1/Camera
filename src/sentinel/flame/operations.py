"""Burn operations: the checklist, crew and equipment, weather against the burn
boss's own limits, smoke direction, and how heat is classified against a field.

Sentinel records and checks what people tell it and what its sensors show. It
never decides that a burn is legal, approved or safe to light: the burn boss
does, under the permit's conditions. Every readiness view says so.

Standard library only."""

from __future__ import annotations

import math
import re
import time
import uuid
from datetime import date
from typing import Any

STATUSES = {
    "planning": "Planning",
    "monitoring": "Monitoring",
    "post_burn": "Post-burn inspection",
    "stood_down": "Stood down",
    "closed": "Closed",
}
OPEN_STATUSES = ("planning", "monitoring", "post_burn", "stood_down")
ACTIVE_STATUSES = ("monitoring", "post_burn")

ROLES = {
    "burn_boss": "Burn boss (in charge)", "ignition": "Ignition crew", "holding": "Holding crew (fire line)",
    "drone_pilot": "Drone pilot", "visual_observer": "Visual observer", "water": "Water tender", "other": "Other",
}
REQUIRED_ROLES = ("burn_boss", "holding", "drone_pilot")

QUALIFICATION_KINDS = {
    "rpas_basic": "Transport Canada drone pilot certificate: basic",
    "rpas_advanced": "Transport Canada drone pilot certificate: advanced",
    "rpas_level1_complex": "Transport Canada drone pilot certificate: Level 1 Complex",
    "burn_training": "Prescribed or agricultural burn training",
    "first_aid": "First aid",
    "other": "Other",
}
PILOT_QUALIFICATIONS = ("rpas_basic", "rpas_advanced", "rpas_level1_complex")

AUTHORIZATION_KINDS = {
    "burn_permit": "Burn permit (municipal or provincial)",
    "daily_authorization": "Daily burning authorization (for example Manitoba's daily crop-residue authorization)",
    "fire_department_notified": "Fire department or dispatch notified",
    "landowner_permission": "Landowner permission",
    "aviation_approval": "Aviation approval (SFOC, RPOC or airspace authorization)",
    "insurance": "Insurance",
    "other": "Other",
}

# Items marked `auto` are checked from live data; the rest are confirmed by a person.
CHECKLIST: list[dict[str, Any]] = [
    {"id": "authorization", "group": "Permits and notice", "label": "Burn permit or daily authorization recorded, and its conditions read",
     "auto": "authorization"},
    {"id": "fire_department", "group": "Permits and notice", "label": "Fire department or dispatch told about the burn"},
    {"id": "neighbours", "group": "Permits and notice", "label": "Neighbours and adjacent landowners told"},
    {"id": "fireguard", "group": "Site", "label": "Fireguard (cultivated or mowed strip) around the field, width recorded on the field"},
    {"id": "hazards", "group": "Site", "label": "Power lines, fuel, buildings, bales and shelterbelts near the field identified"},
    {"id": "escape_routes", "group": "Site", "label": "Escape routes and safety zones agreed with the crew"},
    {"id": "prescription", "group": "Weather", "label": "Burn boss set the weather limits for this burn (from the permit conditions)",
     "auto": "prescription"},
    {"id": "weather", "group": "Weather", "label": "Weather observed in the last hour and within the limits set", "auto": "weather"},
    {"id": "smoke", "group": "Weather", "label": "Smoke will not drift over roads, towns or homes with the observed wind", "auto": "smoke"},
    {"id": "briefing", "group": "Crew", "label": "Crew briefed: roles, radio channel, ignition plan, stop conditions"},
    {"id": "communications", "group": "Crew", "label": "Radios or phones checked between all crew members"},
    {"id": "crew_roles", "group": "Crew", "label": "Burn boss, holding crew and drone pilot named", "auto": "crew_roles"},
    {"id": "equipment", "group": "Equipment", "label": "Water, tillage and hand equipment on site and checked", "auto": "equipment"},
    {"id": "pilot", "group": "Drone", "label": "Drone pilot's certificate recorded and valid", "auto": "pilot"},
    {"id": "airspace", "group": "Drone", "label": "Airspace checked (NAV CANADA drone site tool, NOTAMs, forest-fire restriction areas)"},
    {"id": "drone_configuration", "group": "Drone", "label": "Drone in its agricultural configuration with the accessory attached",
     "auto": "drone_configuration"},
    {"id": "drone_ready", "group": "Drone", "label": "Drone readiness checks passing", "auto": "drone_ready"},
    {"id": "field_in_geofence", "group": "Drone", "label": "Field and watch strip inside the drone's geofence", "auto": "field_in_geofence"},
]
CHECKLIST_INDEX = {item["id"]: item for item in CHECKLIST}

DEFAULT_EQUIPMENT = [
    {"id": "water", "label": "Water tank or sprayer, full, pump tested"},
    {"id": "tillage", "label": "Disc or cultivator hitched (to widen a fireguard)"},
    {"id": "hand_tools", "label": "Shovels, rakes, swatters"},
    {"id": "extinguishers", "label": "Extinguishers on every vehicle"},
    {"id": "radios", "label": "Radios or phones, charged"},
    {"id": "ppe", "label": "Protective clothing for the crew"},
    {"id": "first_aid", "label": "First-aid kit"},
    {"id": "drone_batteries", "label": "Charged drone batteries for the planned watch time"},
]

# Example limits, shown until the burn boss saves their own. Not advice: the
# permit's conditions and the burn boss decide.
EXAMPLE_PRESCRIPTION = {"min_wind_mps": 1.5, "max_wind_mps": 6.0, "max_gust_mps": 9.0, "min_humidity_pct": 25.0,
                        "max_temperature_c": 30.0, "smoke_check_km": 5.0, "smoke_sector_deg": 30.0}
WEATHER_TTL_S = 3600
MIN_OUTSIDE_M = 3.0  # heat must be at least this far (and beyond its position uncertainty) outside to be called outside

DISCLAIMER = ("Sentinel records what people report and what its thermal sensor observes. It does not approve burns, does not judge "
              "burn safety or fire containment, and never controls ignition.")
READINESS_NOTE = "Sentinel does not approve burns or decide when to light; the burn boss decides under the permit's conditions."
# Claims Sentinel must never make about a burn, even negated (tests scan everything it generates).
FORBIDDEN_CLAIMS = (r"safe to (burn|light|ignite|start)", r"approved to burn", r"burn is (approved|legal|safe)", r"legally approved",
                    r"you (may|can) (now )?light", r"go ahead and light", r"cleared to burn", r"fire is (under control|contained|extinguished)",
                    r"fire is out(?!side| of)", r"(field|property|farm) is (safe|protected)")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class OperationError(ValueError):
    """A request the person must correct."""


def forbidden_claims(text: str) -> list[str]:
    """Which forbidden claims appear in `text` (empty when none)."""
    lowered = text.lower()
    return [pattern for pattern in FORBIDDEN_CLAIMS if re.search(pattern, lowered)]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def parse_date(value: Any, what: str, *, required: bool = False) -> str:
    if value in (None, ""):
        if required:
            raise OperationError(f"{what} is required (YYYY-MM-DD)")
        return ""
    if not isinstance(value, str) or not _DATE.match(value):
        raise OperationError(f"{what} must be a date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise OperationError(f"{what} is not a real date") from None
    return value


def clean_text(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x1f]+", " ", str(value or "")).strip()[:limit]


# ---- records: qualifications and authorizations ---------------------------------------


def qualification(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    kind = payload.get("kind")
    if kind not in QUALIFICATION_KINDS:
        raise OperationError(f"kind must be one of {list(QUALIFICATION_KINDS)}")
    person = clean_text(payload.get("person"), 60)
    if not person:
        raise OperationError("whose qualification is it? (person)")
    return {"record_id": new_id("qual"), "kind": "qualification", "recorded_ms": now_ms(), "recorded_by": actor, "person": person,
            "qualification": kind, "reference": clean_text(payload.get("reference"), 60), "expires": parse_date(payload.get("expires"), "expires"),
            "note": clean_text(payload.get("note"), 300), "verified_by_sentinel": False}


def authorization(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    kind = payload.get("kind")
    if kind not in AUTHORIZATION_KINDS:
        raise OperationError(f"kind must be one of {list(AUTHORIZATION_KINDS)}")
    valid_from = parse_date(payload.get("valid_from"), "valid_from")
    valid_until = parse_date(payload.get("valid_until"), "valid_until", required=kind in ("burn_permit", "daily_authorization"))
    if kind == "daily_authorization" and valid_from and valid_from != valid_until:
        raise OperationError("a daily authorization is valid for one date: set valid_from and valid_until to the same day")
    if valid_from and valid_until and valid_until < valid_from:
        raise OperationError("valid_until is before valid_from")
    return {"record_id": new_id("auth"), "kind": "authorization", "recorded_ms": now_ms(), "recorded_by": actor, "authorization": kind,
            "reference": clean_text(payload.get("reference"), 80), "issued_by": clean_text(payload.get("issued_by"), 80),
            "valid_from": valid_from or valid_until, "valid_until": valid_until, "field_id": clean_text(payload.get("field_id"), 41),
            "conditions": clean_text(payload.get("conditions"), 500), "verified_by_sentinel": False}


def valid_on(record: dict[str, Any], day: str) -> bool:
    if record.get("kind") == "qualification":
        return not record.get("expires") or record["expires"] >= day
    start, end = record.get("valid_from") or "", record.get("valid_until") or ""
    return (not start or start <= day) and (not end or end >= day)


# ---- operations -------------------------------------------------------------------------


def new_operation(field: dict[str, Any], drone_id: str, actor: str, *, simulated: bool, name: str = "") -> dict[str, Any]:
    now = now_ms()
    return {
        "operation_id": new_id("burn"), "field_id": field["id"], "field_name": field["name"], "name": clean_text(name, 80) or f"Burn of {field['name']}",
        "created_ms": now, "created_by": actor, "status": "planning", "drone_id": drone_id, "simulated": simulated,
        "prescription": dict(EXAMPLE_PRESCRIPTION) | {"source": "example"},
        "checklist": {}, "crew": [], "equipment": [dict(item, checked=False) for item in DEFAULT_EQUIPMENT],
        "timeline": {}, "acknowledged_incomplete": [], "post_burn": {}, "stand_down": {},
        "counts": {"hotspots_outside": 0, "hotspots_edge": 0, "notifications": 0, "looks": 0},
    }


def set_prescription(operation: dict[str, Any], payload: dict[str, Any], actor: str) -> dict[str, Any]:
    limits = dict(operation["prescription"])
    ranges = {"min_wind_mps": (0, 30), "max_wind_mps": (0, 30), "max_gust_mps": (0, 40), "min_humidity_pct": (0, 100),
              "max_temperature_c": (-40, 50), "smoke_check_km": (0, 50), "smoke_sector_deg": (5, 90)}
    for key, (low, high) in ranges.items():
        if key in payload and payload[key] is not None:
            value = payload[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
                raise OperationError(f"{key} must be between {low} and {high}")
            limits[key] = float(value)
    if limits["min_wind_mps"] > limits["max_wind_mps"]:
        raise OperationError("the lowest wind is above the highest wind")
    limits |= {"source": "burn_boss", "set_by": actor, "set_ms": now_ms()}
    operation["prescription"] = limits
    return limits


def weather_entry(payload: dict[str, Any], actor: str, *, source: str = "observed") -> dict[str, Any]:
    entry: dict[str, Any] = {"source": source, "by": actor}
    for key, low, high in (("wind_mps", 0, 60), ("gust_mps", 0, 80), ("wind_from_deg", 0, 360), ("temperature_c", -50, 60),
                           ("humidity_pct", 0, 100)):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
            raise OperationError(f"{key} must be between {low} and {high}")
        entry[key] = float(value)
    if "wind_mps" not in entry:
        raise OperationError("record at least the wind speed (m/s)")
    entry["note"] = clean_text(payload.get("note"), 200)
    return entry


def assess_weather(entry: dict[str, Any] | None, limits: dict[str, Any]) -> dict[str, Any]:
    """Compare an observation with the limits the burn boss set. Says 'within'
    or 'outside your limits', never 'safe'."""
    if entry is None:
        return {"state": "missing", "detail": "no weather observation recorded", "problems": []}
    problems = []
    wind = entry.get("wind_mps")
    if wind is not None and wind > limits["max_wind_mps"]:
        problems.append(f"wind {wind:g} m/s above your limit of {limits['max_wind_mps']:g}")
    if wind is not None and wind < limits["min_wind_mps"]:
        problems.append(f"wind {wind:g} m/s below your lower limit of {limits['min_wind_mps']:g} (smoke may not lift and disperse)")
    gust = entry.get("gust_mps")
    if gust is not None and gust > limits["max_gust_mps"]:
        problems.append(f"gusts {gust:g} m/s above your limit of {limits['max_gust_mps']:g}")
    humidity = entry.get("humidity_pct")
    if humidity is not None and humidity < limits["min_humidity_pct"]:
        problems.append(f"humidity {humidity:g}% below your limit of {limits['min_humidity_pct']:g}%")
    temperature = entry.get("temperature_c")
    if temperature is not None and temperature > limits["max_temperature_c"]:
        problems.append(f"temperature {temperature:g} °C above your limit of {limits['max_temperature_c']:g} °C")
    missing = [label for key, label in (("humidity_pct", "humidity"), ("wind_from_deg", "wind direction")) if key not in entry]
    if problems:
        return {"state": "fail", "detail": "; ".join(problems), "problems": problems}
    detail = "within the limits set" + (" (example limits: the burn boss has not set them)" if limits.get("source") == "example" else "")
    if missing:
        return {"state": "warn", "detail": f"{detail}; not recorded: {', '.join(missing)}", "problems": []}
    return {"state": "pass", "detail": detail, "problems": []}


def smoke_toward(field_view: dict[str, Any], wind_from_deg: float | None, limits: dict[str, Any]) -> dict[str, Any]:
    """Sensitive places downwind of the field within the check distance."""
    from ..drone.base import geo_to_offset

    if wind_from_deg is None:
        return {"state": "missing", "detail": "wind direction not recorded", "places": []}
    centre = field_view["centre"]
    smoke_to = (wind_from_deg + 180) % 360
    places = []
    for place in field_view.get("sensitive", []):
        east, north = geo_to_offset(tuple(centre), *place["point"])
        distance_km = math.hypot(east, north) / 1000
        bearing = math.degrees(math.atan2(east, north)) % 360
        off = abs((bearing - smoke_to + 180) % 360 - 180)
        if distance_km <= limits["smoke_check_km"] and off <= limits["smoke_sector_deg"]:
            places.append({"label": place["label"], "kind": place["kind"], "distance_km": round(distance_km, 2), "off_axis_deg": round(off)})
    if not field_view.get("sensitive"):
        return {"state": "warn", "detail": "no sensitive places marked on the field: add homes, roads and towns nearby", "places": []}
    if places:
        names = ", ".join(f"{p['label']} ({p['distance_km']:g} km)" for p in places)
        return {"state": "fail", "detail": f"smoke would drift toward {names}", "places": places}
    return {"state": "pass", "detail": f"no marked place within {limits['smoke_check_km']:g} km downwind (smoke toward {smoke_to:.0f}°)", "places": []}


def classify(signed_m: float, uncertainty_m: float | None, field: dict[str, Any]) -> dict[str, Any]:
    """Where heat is relative to the boundary. Negative distances are inside.
    Heat is called outside only when it is outside by more than its position
    uncertainty; otherwise it is reported at the boundary."""
    margin = max(float(uncertainty_m or 0.0), MIN_OUTSIDE_M)
    if signed_m > margin:
        zone = "outside"
    elif signed_m > -field["edge_m"]:
        zone = "edge"
    else:
        zone = "interior"
    return {"zone": zone, "distance_m": round(signed_m, 1), "beyond_watch_strip": signed_m > field["watch_buffer_m"]}


def readiness(operation: dict[str, Any], auto: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Checklist with auto items from live data. Returns counts and a
    statement that never claims approval or safety."""
    items = []
    for item in CHECKLIST:
        if item.get("auto"):
            result = auto.get(item["auto"], {"state": "missing", "detail": "not checked"})
            items.append(item | {"state": result["state"], "detail": result["detail"], "by": "Sentinel (live data)"})
            continue
        record = operation["checklist"].get(item["id"]) or {}
        state = {"done": "pass", "not_applicable": "na"}.get(record.get("status", ""), "missing")
        detail = record.get("note") or ("" if state != "missing" else "not confirmed")
        items.append(item | {"state": state, "detail": detail, "by": record.get("by", ""), "ms": record.get("ms")})
    incomplete = [item for item in items if item["state"] in ("missing", "fail")]
    warnings = [item for item in items if item["state"] == "warn"]
    done = sum(1 for item in items if item["state"] in ("pass", "na"))
    statement = (f"{done} of {len(items)} checklist items recorded" + (f"; {len(incomplete)} open" if incomplete else "")
                 + (f"; {len(warnings)} with warnings" if warnings else "") + f". {READINESS_NOTE}")
    return {"items": items, "done": done, "total": len(items), "incomplete": [item["id"] for item in incomplete],
            "warnings": [item["id"] for item in warnings], "statement": statement}


def crew_roles(operation: dict[str, Any]) -> dict[str, Any]:
    named = {member["role"] for member in operation["crew"]}
    missing = [ROLES[role] for role in REQUIRED_ROLES if role not in named]
    if missing:
        return {"state": "missing", "detail": "not named: " + ", ".join(missing)}
    return {"state": "pass", "detail": f"{len(operation['crew'])} crew member(s) named"}


def equipment_state(operation: dict[str, Any]) -> dict[str, Any]:
    unchecked = [item["label"] for item in operation["equipment"] if not item.get("checked")]
    if unchecked:
        return {"state": "missing", "detail": f"{len(unchecked)} not checked: " + "; ".join(unchecked[:4]) + ("…" if len(unchecked) > 4 else "")}
    return {"state": "pass", "detail": f"all {len(operation['equipment'])} items checked"}


def pilot_state(operation: dict[str, Any], qualifications: list[dict[str, Any]], day: str) -> dict[str, Any]:
    pilots = [member["name"] for member in operation["crew"] if member["role"] == "drone_pilot"]
    if not pilots:
        return {"state": "missing", "detail": "no drone pilot named"}
    for pilot in pilots:
        valid = [q for q in qualifications if q["person"].lower() == pilot.lower() and q["qualification"] in PILOT_QUALIFICATIONS and valid_on(q, day)]
        if valid:
            best = valid[0]
            return {"state": "pass", "detail": f"{pilot}: {QUALIFICATION_KINDS[best['qualification']]}"
                    + (f", valid to {best['expires']}" if best.get("expires") else "") + " (recorded, not verified by Sentinel)"}
    return {"state": "missing", "detail": f"no valid drone pilot certificate recorded for {', '.join(pilots)}"}


def authorization_state(records: list[dict[str, Any]], field_id: str, day: str) -> dict[str, Any]:
    relevant = [r for r in records if r["authorization"] in ("burn_permit", "daily_authorization")
                and (not r.get("field_id") or r["field_id"] == field_id)]
    current = [r for r in relevant if valid_on(r, day)]
    if current:
        record = current[0]
        return {"state": "pass", "detail": f"{AUTHORIZATION_KINDS[record['authorization']].split(' (')[0]} {record.get('reference') or ''} "
                                            f"recorded by {record['recorded_by']}, valid to {record['valid_until']} (not verified by Sentinel)".replace("  ", " ")}
    if relevant:
        return {"state": "fail", "detail": "the recorded permit or authorization is not valid today"}
    return {"state": "missing", "detail": "no burn permit or daily authorization recorded"}


__all__ = [
    "ACTIVE_STATUSES", "AUTHORIZATION_KINDS", "CHECKLIST", "CHECKLIST_INDEX", "DEFAULT_EQUIPMENT", "DISCLAIMER", "EXAMPLE_PRESCRIPTION",
    "FORBIDDEN_CLAIMS", "OPEN_STATUSES", "OperationError", "QUALIFICATION_KINDS", "READINESS_NOTE", "REQUIRED_ROLES", "ROLES", "STATUSES",
    "assess_weather", "authorization", "authorization_state", "classify", "crew_roles", "equipment_state", "new_operation", "pilot_state",
    "qualification", "readiness", "set_prescription", "smoke_toward", "valid_on", "weather_entry",
]
