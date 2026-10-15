"""Removable drone accessories: what can be fitted to which Sentinel aircraft,
what it costs the aircraft, and when the aircraft may switch from its security
configuration to an agricultural one.

One aircraft architecture. An accessory is registered in `devices[]` as
`category: accessory` with the drone it is fitted to (`drone_id`). Without an
accessory the aircraft is the ordinary Sentinel security drone.

Boundaries:
- **Monitoring only.** Sentinel never controls ignition, incendiary-release or
  any other release hardware. Ignition payloads exist here only as evaluation
  records (support `unsupported`), so they cannot be registered as installed.
- **Registration is not attachment.** A registered accessory is `attached` only
  when Sentinel receives a report saying so (today: the simulated kit).
- Masses, powers and limits are labelled `published` (manufacturer or
  distributor listing, with its source) or `estimate`. A commissioning record
  with a measured mass replaces the estimate. Nothing here is a flight test.

Standard library only."""

from __future__ import annotations

import copy
import math
from typing import Any

from . import catalog

APPLICATIONS = {
    "security": "Security inspections (no accessory needed)",
    "agricultural_burn_monitoring": "Agricultural burn monitoring: thermal watch of a field burn, observations and reports",
}
CONFIGURATIONS = {
    "security": "Security",
    "agricultural_monitoring": "Agricultural monitoring",
}

# Airframe limits, from the manufacturer. Only what is published is filled in.
AIRFRAMES: dict[str, dict[str, Any]] = {
    "x650": {
        "name": "Holybro X650", "kit_mass_g": 2000, "max_takeoff_g": 6300, "max_takeoff_basis": "at 70% throttle",
        "payload_note": "4.3 kg without battery, 3.5 kg with a 5.2 Ah (750 g) pack, 3.1 kg with a 10 Ah (1200 g) pack",
        "hover_min_no_payload": 30, "battery": "6S 5-12 Ah",
        "source": "Holybro X650 development kit documentation (checked 2026-10-14)",
    },
    "x500_v2": {
        "name": "Holybro X500 V2", "kit_mass_g": 610, "max_takeoff_g": None, "max_takeoff_basis": "not published",
        "payload_note": "about 1-1.5 kg (manufacturer range; no take-off limit published)", "hover_min_no_payload": 18, "battery": "4S",
        "source": "Holybro X500 V2 listing via docs/drone-hardware-v1.md",
    },
}
MODEL_AIRFRAME = {"sentinel_lite": "x500_v2", "sentinel_pro": "x650", "sentinel_thermal": "x650"}

# Engineering margins. Estimates until flight tests replace them.
DESIGN_MASS_FRACTION = 0.8  # plan to at most 80% of the published take-off limit (wind, gusts, battery sag)
RESERVE_PCT = 30.0  # landing reserve, as for inspections
MIN_ON_STATION_MIN = 10.0  # a useful thermal watch of a field edge needs at least this long between battery swaps
ENDURANCE_EXPONENT = 1.5  # hover power scales roughly with mass^1.5 at a fixed battery

SUPPORT_NOTE = ("Accessory support levels follow the device registry: `simulated` works with Sentinel's simulator only, `planned` is a "
                "reference design that has not been built, `unsupported` is recorded for evaluation and cannot be installed.")

PROFILES: list[dict[str, Any]] = [
    {
        "id": "simulated_burn_monitor_kit", "name": "Simulated burn-monitoring kit", "kind": "monitoring_kit", "support": "simulated",
        "applications": ["agricultural_burn_monitoring"], "ignition": False, "vendor": "Sentinel (simulator)",
        "summary": "Stands in for the burn-monitoring kit so the agricultural workflow can be exercised end to end. Reports attachment, "
                   "identification, latch and heat-shield temperatures; faults can be injected.",
        "mass": {"value_g": 250, "basis": "estimate", "source": "same estimate as the Sentinel burn-monitoring kit reference design"},
        "power": {"watts": 2.0, "supply": "5 V from the companion computer", "basis": "estimate"},
        "interface": {"mount": "quick-release lower plate on the aircraft centreline", "identification": "ID chip read by the companion",
                      "data": "companion", "control": "none (monitoring only)"},
        "requires": {"capabilities": ["thermal", "companion_computer"], "configuration": "agricultural_monitoring"},
        "health": ["attached", "latch_closed", "identity_ok", "bay_temperature_c", "shield_temperature_c"],
        "cg": {"offset_mm_est": 0, "note": "centreline mount below the battery"},
        "max_temperature_c": 60.0,
        "notes": ["Simulated: exercises the software, watches nothing real."],
    },
    {
        "id": "sentinel_burn_monitor_kit", "name": "Sentinel burn-monitoring kit", "kind": "monitoring_kit", "support": "planned",
        "applications": ["agricultural_burn_monitoring"], "ignition": False, "vendor": "Sentinel (reference design)",
        "summary": "Uses the aircraft's existing ZT6 thermal camera. Adds a quick-release lower plate with an ID chip, a reflective heat "
                   "shield over the battery and gimbal bay, two temperature probes read by the CM4, and high-visibility markings.",
        "mass": {"value_g": 250, "basis": "estimate", "source": "parts not chosen; weigh the first build"},
        "power": {"watts": 2.0, "supply": "5 V from the CM4 baseboard", "basis": "estimate"},
        "interface": {"mount": "quick-release lower plate on the aircraft centreline", "identification": "1-Wire or I2C ID chip read by the CM4",
                      "data": "companion", "control": "none (monitoring only)"},
        "requires": {"capabilities": ["thermal", "companion_computer"], "configuration": "agricultural_monitoring"},
        "health": ["attached", "latch_closed", "identity_ok", "bay_temperature_c", "shield_temperature_c"],
        "cg": {"offset_mm_est": 0, "note": "centreline mount below the battery"},
        "max_temperature_c": 60.0,
        "notes": ["Reference design: nothing has been built. Registration only until the hardware engineer builds and bench-tests it.",
                  "The companion agent does not yet report accessory health; that report is part of the build."],
    },
    {
        "id": "ignis_iii_mini", "name": "Drone Amplified IGNIS III Mini (aerial ignition)", "kind": "aerial_ignition", "support": "unsupported",
        "applications": [], "ignition": True, "vendor": "Drone Amplified",
        "summary": "Commercial aerial-ignition payload for prescribed fire, sold integrated with the Freefly Astro and controlled from the "
                   "vendor's D.A. FLIGHT app. Recorded here as an engineering reference only.",
        "mass": {"value_g": 1870, "basis": "published", "empty_g": 1250,
                 "source": "Drone Amplified IGNIS III page: 1.87 kg with 225 spheres and antifreeze; 1.25 kg empty including the LYNX EO/IR camera"},
        "power": {"watts": None, "supply": "through the aircraft's smart dovetail mount", "basis": "not published"},
        "interface": {"mount": "smart dovetail electro-mechanical connection (Freefly Astro)", "identification": "vendor",
                      "data": "proprietary (D.A. FLIGHT app)", "control": "vendor app on the Pilot Pro controller"},
        "requires": {"capabilities": [], "configuration": ""},
        "health": [],
        "cg": {"offset_mm_est": None, "note": "not published"},
        "published": {
            "capacity": "225 spheres, 19 mm, 2.4 g each; up to 120 drops per minute; reload 2-5 min",
            "ignition_delay": "spheres ignite 20-40 s after injection (0.2 mL, varies with temperature)",
            "aircraft": "Freefly Astro with extended landing gear: 3.52 kg empty, 7.46 kg with batteries, camera and payload, 22-30 min loaded",
            "camera": "LYNX EO/IR: radiometric 640x512 thermal (<50 mK), 4K visible",
            "safety": "an IGNIS safety geofence set in the vendor app before take-off",
            "logistics": "three cases, 92.3 lb",
            "training": "vendor training (flight operations, software, safety procedures, maintenance) and a field guide",
            "ip": "patented and patent pending; exclusive licence from NUTech Ventures",
        },
        "max_temperature_c": None,
        "notes": ["Not installable on any Sentinel aircraft: Sentinel never controls ignition or release hardware.",
                  "Integrated with the Freefly Astro through a proprietary mount and app; no published third-party interface.",
                  "On the X650 its loaded mass would leave almost no margin (see the compatibility evaluation)."],
    },
    {
        "id": "aerial_ignition_other", "name": "Other professionally approved aerial-ignition payload", "kind": "aerial_ignition",
        "support": "unsupported", "applications": [], "ignition": True, "vendor": "various",
        "summary": "Any sphere-dispenser or drip-torch payload approved for prescribed fire (for example the IGNIS II on the Freefly Alta X or "
                   "Inspired Flight IF1200A). Recorded for evaluation only.",
        "mass": {"value_g": None, "basis": "not published", "source": ""},
        "power": {"watts": None, "supply": "vendor", "basis": "not published"},
        "interface": {"mount": "vendor", "identification": "vendor", "data": "proprietary", "control": "vendor controller"},
        "requires": {"capabilities": [], "configuration": ""},
        "health": [], "cg": {"offset_mm_est": None, "note": "vendor"}, "published": {}, "max_temperature_c": None,
        "notes": ["Sentinel never controls ignition or release hardware. At most, a later product decision could let Sentinel read a vendor's "
                  "status (read-only); that decision must be recorded in CLAUDE.md first."],
    },
]
PROFILE_INDEX = {profile["id"]: profile for profile in PROFILES}
RECORD_KINDS = ("inspection", "maintenance", "repair", "commissioning", "attached", "detached", "fault", "note")
FAULTS = {
    "detached": "accessory not detected on the mount",
    "latch_open": "mount latch reported open",
    "identity_mismatch": "identification does not match the registered accessory",
    "overheat": "heat-shield temperature above the accessory's limit",
    "silent": "no reports from the accessory",
}


def public_profiles() -> list[dict[str, Any]]:
    return copy.deepcopy(PROFILES)


def profile_for(device: dict[str, Any] | None) -> dict[str, Any] | None:
    return PROFILE_INDEX.get((device or {}).get("profile", ""))


def validate_accessory(device: dict[str, Any], path: str, devices: list[dict[str, Any]], errors: list[str]) -> None:
    """Fields of a registered accessory (called from devices.validate_devices)."""
    drone_id = device.get("drone_id")
    drones = {d.get("id") for d in devices if d.get("category") == "drone"}
    if not drone_id:
        errors.append(f"{path}.drone_id must name the drone the accessory is fitted to")
    elif drone_id not in drones:
        errors.append(f"{path}.drone_id references unknown drone {drone_id!r}")
    serial = device.get("serial")
    if serial is not None and not (isinstance(serial, str) and 0 < len(serial) <= 40):
        errors.append(f"{path}.serial must be 1-40 characters")
    profile = PROFILE_INDEX.get(device.get("profile", ""))
    if profile and profile["ignition"]:
        errors.append(f"{path}: {profile['name']} is an ignition payload; Sentinel never controls ignition or release hardware")
    others = [d for d in devices if d is not device and d.get("category") == "accessory" and drone_id and d.get("drone_id") == drone_id]
    if others:
        errors.append(f"{path}: drone {drone_id!r} already has accessory {others[0].get('id')!r} (one removable accessory per aircraft)")


# ---- compatibility --------------------------------------------------------------------


def _check(check_id: str, label: str, state: str, detail: str, source: str = "") -> dict[str, Any]:
    return {"id": check_id, "label": label, "state": state, "detail": detail, "source": source}


def endurance_with_payload(model: dict[str, Any], payload_g: float) -> dict[str, Any]:
    """Estimated hover time with an extra payload on the same battery."""
    base_min = float(model["flight"]["endurance_min_est"] or 0)
    base_kg = float(model["flight"]["takeoff_mass_kg_est"] or 0)
    if not base_min or not base_kg:
        return {"known": False, "detail": "endurance unknown for this aircraft"}
    total_kg = base_kg + payload_g / 1000.0
    minutes = base_min * (base_kg / total_kg) ** ENDURANCE_EXPONENT
    usable = minutes * (1 - RESERVE_PCT / 100)
    return {"known": True, "base_min": base_min, "with_payload_min": round(minutes, 1), "usable_min": round(usable, 1),
            "takeoff_kg": round(total_kg, 2), "basis": "estimate",
            "detail": (f"about {minutes:.0f} min of hover (estimate, from {base_min:g} min at {base_kg:g} kg, hover power ~ mass^1.5); "
                       f"about {usable:.0f} min before the {RESERVE_PCT:g}% reserve")}


def model_with_payload(model: dict[str, Any], payload_g: float, label: str) -> dict[str, Any]:
    """The drone model with an accessory fitted: endurance and take-off mass
    follow the payload, so route endurance checks stay honest. Returns the
    model itself when nothing is fitted."""
    if payload_g <= 0:
        return model
    adjusted = copy.deepcopy(model)
    estimate = endurance_with_payload(model, payload_g)
    if estimate["known"]:
        adjusted["flight"]["endurance_min_est"] = estimate["with_payload_min"]
        adjusted["flight"]["takeoff_mass_kg_est"] = estimate["takeoff_kg"]
        adjusted["flight"]["source"] = f"{model['flight']['source']}; adjusted for {label} ({payload_g:g} g)"
    adjusted["payload"] = {"label": label, "mass_g": payload_g}
    return adjusted


def evaluate(profile: dict[str, Any], model: dict[str, Any], *, measured_mass_g: float | None = None) -> dict[str, Any]:
    """Can this accessory be carried by this aircraft, and what has to be
    validated first? Never a flight clearance: every result lists what is an
    estimate and what an engineer must measure."""
    checks: list[dict[str, Any]] = []
    airframe_id = MODEL_AIRFRAME.get(model["id"])
    airframe = AIRFRAMES.get(airframe_id or "")
    if profile["ignition"]:
        checks.append(_check("monitoring_only", "Monitoring only", "fail",
                             "ignition payload: Sentinel never controls ignition or release hardware, and does not integrate it"))
    mass_g = measured_mass_g if measured_mass_g is not None else profile["mass"].get("value_g")
    mass_basis = "measured" if measured_mass_g is not None else profile["mass"]["basis"]
    base_kg = float(model["flight"].get("takeoff_mass_kg_est") or 0)
    mass: dict[str, Any] = {"accessory_g": mass_g, "accessory_basis": mass_basis, "aircraft_kg_est": base_kg or None}
    if airframe is None:
        checks.append(_check("airframe", "Airframe limits", "unverified", f"{model['name']}: no published airframe limits; weigh and test"))
    elif mass_g is None:
        checks.append(_check("mass", "Take-off mass", "unverified", "accessory mass not published", profile["mass"].get("source", "")))
    elif airframe["max_takeoff_g"] is None:
        checks.append(_check("mass", "Take-off mass", "unverified",
                             f"{airframe['name']}: {airframe['payload_note']}; with this accessory about {base_kg + mass_g / 1000:.2f} kg",
                             airframe["source"]))
    else:
        total_g = base_kg * 1000 + mass_g
        limit_g = airframe["max_takeoff_g"]
        design_g = limit_g * DESIGN_MASS_FRACTION
        mass |= {"takeoff_g": round(total_g), "limit_g": limit_g, "design_limit_g": round(design_g), "margin_g": round(limit_g - total_g),
                 "design_margin_g": round(design_g - total_g), "fraction_of_limit": round(total_g / limit_g, 2)}
        state = "pass" if total_g <= design_g else "warn" if total_g <= limit_g else "fail"
        checks.append(_check(
            "mass", "Take-off mass", state,
            f"about {total_g / 1000:.2f} kg ({base_kg:g} kg aircraft estimate + {mass_g / 1000:.2f} kg accessory, {mass_basis}) against "
            f"{limit_g / 1000:g} kg {airframe['max_takeoff_basis']} ({total_g / limit_g:.0%}); planning limit {design_g / 1000:.2f} kg "
            f"({DESIGN_MASS_FRACTION:.0%})", airframe["source"]))
    if mass_g is not None and not profile["ignition"]:
        endurance = endurance_with_payload(model, mass_g)
        mass["endurance"] = endurance
        if endurance["known"]:
            state = "pass" if endurance["usable_min"] >= MIN_ON_STATION_MIN else "warn"
            checks.append(_check("endurance", "Flight time", state, endurance["detail"]
                                 + ("" if state == "pass" else f": less than {MIN_ON_STATION_MIN:g} min of useful watch per battery")))
        else:
            checks.append(_check("endurance", "Flight time", "unverified", endurance["detail"]))
    elif mass_g is not None:
        endurance = endurance_with_payload(model, mass_g)
        mass["endurance"] = endurance
    required = profile["requires"]["capabilities"]
    missing = [cap for cap in required if model["capabilities"].get(cap) not in ("yes",)]
    if required:
        checks.append(_check("sensors", "Sensors the accessory relies on", "fail" if missing else "pass",
                             ("missing on the " + model["name"] + ": " + ", ".join(catalog.CAPABILITIES[c]["label"] for c in missing))
                             if missing else "thermal camera and companion computer are part of the " + model["name"]))
    data = profile["interface"]["data"]
    if data == "companion":
        checks.append(_check("data", "Status reports to Sentinel", "unverified" if profile["support"] != "simulated" else "pass",
                             "read-only health through the companion computer" + ("" if profile["support"] == "simulated"
                                                                                  else " (agent support not built yet)")))
    elif data.startswith("proprietary"):
        checks.append(_check("data", "Status reports to Sentinel", "fail", f"{data}: no published interface Sentinel could read"))
    watts = profile["power"].get("watts")
    checks.append(_check("power", "Electrical supply", "unverified" if watts is None or profile["support"] != "simulated" else "pass",
                         f"{watts:g} W from {profile['power']['supply']} ({profile['power']['basis']})" if watts is not None
                         else f"power draw not published ({profile['power']['supply']})"))
    offset = profile["cg"].get("offset_mm_est")
    checks.append(_check("balance", "Centre of gravity", "unverified",
                         (f"{profile['cg']['note']}; measure the CG with the accessory and a full battery fitted, and re-tune "
                          "(AutoTune) if hover throttle or attitude response changes") if offset is not None
                         else "mount position not published; an off-centre payload needs its own CG measurement and tuning"))
    max_temp = float(model["flight"].get("max_temperature_c", 40))
    checks.append(_check("heat", "Heat, smoke and ash", "unverified",
                         f"the aircraft is rated to {max_temp:g} °C air temperature (estimate): keep the standoff distance and height from "
                         "flames, and inspect motors and the gimbal for ash after each operation; radiant-heat exposure is untested"))
    states = {check["state"] for check in checks}
    verdict = "not_compatible" if "fail" in states else "needs_validation" if states & {"warn", "unverified"} else "compatible"
    return {"profile": profile["id"], "profile_name": profile["name"], "model": model["id"], "model_name": model["name"],
            "airframe": airframe["name"] if airframe else "", "verdict": verdict, "checks": checks, "mass": mass,
            "statement": _statement(verdict, profile, model)}


def _statement(verdict: str, profile: dict[str, Any], model: dict[str, Any]) -> str:
    if verdict == "not_compatible":
        return f"{profile['name']} cannot be used on the {model['name']}."
    if verdict == "needs_validation":
        return f"{profile['name']} fits the {model['name']} on paper; the items marked unverified need an engineer's measurements first."
    return f"{profile['name']} is compatible with the {model['name']} in simulation."


def compatibility_matrix() -> list[dict[str, Any]]:
    """Every accessory against every Sentinel model (the comparison screen and docs)."""
    return [evaluate(profile, catalog.MODEL_INDEX[model_id]) for profile in PROFILES for model_id in catalog.TIERS]


# ---- configuration switching -----------------------------------------------------------


def switch_blockers(target: str, *, telemetry: dict[str, Any], mission_open: bool, open_requests: int, accessory: dict[str, Any] | None,
                    operation_active: bool) -> list[dict[str, str]]:
    """Why the aircraft may not change configuration now. `accessory` is the
    live accessory view (or None). Each blocker is {id, detail}."""
    blockers: list[dict[str, str]] = []
    if target not in CONFIGURATIONS:
        return [{"id": "unknown_configuration", "detail": f"configuration must be one of {list(CONFIGURATIONS)}"}]
    if telemetry.get("in_air") or telemetry.get("armed"):
        blockers.append({"id": "aircraft_not_on_ground", "detail": "land and disarm first"})
    if mission_open:
        blockers.append({"id": "inspection_in_progress", "detail": "an inspection is approved or in progress"})
    if target == "agricultural_monitoring":
        if open_requests:
            blockers.append({"id": "security_requests_open", "detail": f"{open_requests} security inspection request(s) are waiting; approve, deny or let them expire"})
        if accessory is None:
            blockers.append({"id": "no_accessory", "detail": "no agricultural accessory is registered for this drone"})
        else:
            if accessory.get("compatibility", {}).get("verdict") == "not_compatible":
                blockers.append({"id": "accessory_not_compatible", "detail": accessory["compatibility"]["statement"]})
            if accessory.get("attachment") != "attached":
                blockers.append({"id": "accessory_not_attached", "detail": accessory.get("attachment_detail") or "no report that the accessory is attached"})
            for fault in accessory.get("faults", []):
                blockers.append({"id": f"accessory_{fault}", "detail": FAULTS.get(fault, fault)})
            if not accessory.get("commissioned"):
                blockers.append({"id": "accessory_not_commissioned", "detail": "record the accessory's commissioning (weighed, balance checked) first"})
            if accessory.get("inspection_due"):
                blockers.append({"id": "accessory_inspection_due", "detail": "record an inspection of the accessory first"})
    elif operation_active:
        blockers.append({"id": "burn_operation_active", "detail": "a burn operation is being monitored with this drone; close or stand it down first"})
    return blockers


__all__ = [
    "AIRFRAMES", "APPLICATIONS", "CONFIGURATIONS", "FAULTS", "PROFILES", "PROFILE_INDEX", "RECORD_KINDS", "compatibility_matrix",
    "endurance_with_payload", "evaluate", "model_with_payload", "profile_for", "public_profiles", "switch_blockers", "validate_accessory",
]
