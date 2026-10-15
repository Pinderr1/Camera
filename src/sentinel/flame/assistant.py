"""Sentinel AI Guide for Flame Mode: tools on live Flame Mode data, two
proposals the person confirms (save a field, switch the drone's
configuration), and offline answers for the everyday questions.

Same rules as every assistant tool: reads go through existing services,
changes only through confirmed proposals, nothing commands a drone, and
nothing claims that a burn is approved, legal or safe to light."""

from __future__ import annotations

import re
from typing import Any, Callable

from ..assistant.privacy import untrusted
from . import operations as ops

SCREEN = "Flame Mode (agricultural)"
LIMITS = [
    "Sentinel never approves a burn and cannot say whether it is safe to light: the burn boss decides under the permit's conditions.",
    "Sentinel never controls ignition. The crew lights the field; Sentinel records that they reported it.",
    "Thermal observations cover only what the drone camera saw; read the coverage figures before relying on them.",
    "Permits, qualifications and checklist items are recorded as people entered them; Sentinel does not verify them with any authority.",
]
_COORD = re.compile(r"(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})")


def _flame(toolbox: Any) -> Any:
    flame = getattr(toolbox.service, "flame", None)
    if flame is None:
        raise ValueError("Flame Mode is not available in this Sentinel")
    return flame


def _active(flame: Any, operation_id: str | None) -> dict[str, Any] | None:
    if operation_id:
        return flame.operation_view(operation_id)
    rows = [row for row in flame.operations_list(20) if row["status"] != "closed"]
    rows.sort(key=lambda row: (row["status"] not in ops.ACTIVE_STATUSES, -row["created_ms"]))
    return flame.operation_view(rows[0]["operation_id"]) if rows else None


def register_tools(toolbox: Any, add: Callable[..., Any], string: Callable[..., dict[str, Any]], boolean: Callable[..., dict[str, Any]],
                   number: Callable[..., dict[str, Any]]) -> None:
    from ..assistant.tools import ToolError

    def flame() -> Any:
        try:
            return _flame(toolbox)
        except ValueError as error:
            raise ToolError(str(error)) from None

    @add("get_flame_overview", "read", "Flame Mode (the drone's optional agricultural burn-monitoring configuration): fields with area and what is "
         "monitored around them (interior, boundary edge, watch strip, sensitive places), each drone's configuration (security or agricultural "
         "monitoring) and why it cannot switch, registered accessories, and open burn operations.")
    def get_flame_overview(args, turn):
        data = flame().overview()
        turn.open("flame", "Open Flame Mode")
        return {
            "enabled": data["enabled"],
            "recording": data["recording"],
            "fields": [{"id": f["id"], "name": untrusted(f["name"]), "area_ha": f["area_ha"], "perimeter_m": f["perimeter_m"],
                        "watch_strip_m": f["watch_buffer_m"], "fireguard_m": f["fireguard_m"], "monitored_areas": f["monitored_areas"],
                        "sensitive_places": [untrusted(p["label"]) for p in f.get("sensitive", [])], "geofence": f["geofence"]["detail"]}
                       for f in data["fields"]],
            "drones": [{"drone_id": d["drone_id"], "name": untrusted(d["name"]), "configuration": d["label"],
                        "cannot_switch_because": [b["detail"] for b in d["switch_blockers"]], "effects": d["effects"]} for d in data["drones"]],
            "accessories": [{"id": a["id"], "name": untrusted(a["name"]), "support": a["support"], "attachment": a["attachment"],
                             "faults": a["faults"], "commissioned": a["commissioned"], "inspection_due": a["inspection_due"]} for a in data["accessories"]],
            "operations": [{k: o[k] for k in ("operation_id", "status", "field_name")} for o in data["operations"] if o["status"] != "closed"],
            "limits": LIMITS,
        }

    @add("get_drone_sensor_status", "read", "Are the drone's sensors working? Readiness checks of each drone (GPS, navigation, battery, link, "
         "camera stream, companion), the removable accessory's health report (attachment, latch, identity, heat-shield temperature, faults) and "
         "the thermal watch state of open burn operations.")
    def get_drone_sensor_status(args, turn):
        service = toolbox.service
        result = []
        for drone_id, manager in list(service.drones.items()):
            readiness = manager.readiness()
            result.append({"drone_id": drone_id, "aircraft": manager.aircraft_label(),
                           "checks_passing": [c["label"] for c in readiness["checks"] if c["state"] == "pass"],
                           "checks_not_passing": [{"label": c["label"], "state": c["state"], "detail": c["detail"]} for c in readiness["checks"]
                                                  if c["state"] in ("fail", "warn")]})
        accessories = [{"name": untrusted(a["name"]), "support": a["support"], "reporting": a["reporting"], "attachment": a["attachment"],
                        "health": a["health"], "faults": a["faults"], "detail": a["attachment_detail"]} for a in flame().accessories()]
        thermal = []
        for row in flame().operations_list(20):
            if row["status"] in ops.ACTIVE_STATUSES:
                view = flame().operation_view(row["operation_id"], observations=1)
                thermal.append({"operation": untrusted(row["field_name"]), "thermal": view["thermal"], "coverage": view["coverage"]})
        turn.open("drone", "Open the Drone tab")
        return {"drones": result, "accessories": accessories, "thermal_watch": thermal,
                "note": "Passing checks mean Sentinel received good data from that sensor just now; they are not a flight clearance."}

    @add("get_burn_checklist", "read", "The open burn operation's checklist (permits, notice, site, weather against the burn boss's limits, smoke "
         "direction, crew roles, equipment, drone), which items are incomplete, and which equipment has not been checked.",
         {"operation_id": string("Operation id (default: the open operation)")})
    def get_burn_checklist(args, turn):
        view = _active(flame(), args.get("operation_id"))
        if view is None:
            raise ToolError("no open burn operation: create one for a field on the Flame tab")
        readiness = view["readiness"] or {"items": [], "statement": ""}
        turn.open("flame", "Open the operation")
        return {"operation": untrusted(view["name"]), "status": view["status_label"], "statement": readiness["statement"],
                "incomplete": [{"label": i["label"], "state": i["state"], "detail": untrusted(i["detail"], 200)} for i in readiness["items"]
                               if i["state"] in ("missing", "fail", "warn")],
                "equipment_not_checked": [untrusted(e["label"]) for e in view["equipment"] if not e.get("checked")],
                "prescription": {k: v for k, v in view["prescription"].items() if k not in ("set_ms",)}, "limits": LIMITS[:2]}

    @add("get_hotspots", "read", "Heat the thermal watch observed in an operation, grouped by where it is: outside the field boundary, at the "
         "boundary edge, near a sensitive place, inside, or remaining after the burn; with position uncertainty and how much of the boundary "
         "and watch strip the camera covered recently.",
         {"operation_id": string("Operation id (default: the open operation)"), "outside_only": boolean("Only heat outside or at the boundary")})
    def get_hotspots(args, turn):
        view = _active(flame(), args.get("operation_id"))
        if view is None:
            raise ToolError("no open burn operation")
        kinds = {"heat_outside_boundary": "outside", "heat_at_boundary": "edge", "heat_near_sensitive_place": "sensitive",
                 "residual_heat": "remaining", "hotspot": "inside"}
        rows = [o for o in view["observations"] if o["kind"] in kinds and (not args.get("outside_only") or kinds[o["kind"]] in ("outside", "edge", "sensitive"))]
        turn.open("flame", "Open the monitoring map")
        return {"operation": untrusted(view["name"]), "status": view["status_label"], "simulated": view["simulated"],
                "counts": {label: sum(1 for o in rows if kinds[o["kind"]] == label) for label in set(kinds.values())},
                "latest": [{"where": kinds[o["kind"]], "text": o["text"], "time_ms": o["recorded_ms"], "severity": o["severity"]} for o in rows[-12:]],
                "coverage": view["coverage"], "thermal": view["thermal"],
                "note": "No heat reported outside means none was seen where the camera looked, not that there is none."}

    @add("get_operation_report", "read", "Summary of a burn operation's report (timeline, checklist statement, weather, alerts, thermal counts, "
         "post-burn result, fingerprint) and a button that opens the printable report.",
         {"operation_id": string("Operation id (default: the most recent operation)")})
    def get_operation_report(args, turn):
        operation_id = args.get("operation_id")
        if not operation_id:
            rows = flame().operations_list(1)
            if not rows:
                raise ToolError("no burn operations recorded yet")
            operation_id = rows[0]["operation_id"]
        report = flame().report(operation_id)
        turn.open("flame_report", "Open the printable report", operation_id=operation_id)
        return {"operation": untrusted(report["operation"]["name"]), "status": report["operation"]["status"], "simulated": report["simulated"],
                "timeline": report["operation"]["timeline"], "checklist": report["readiness"]["statement"], "weather_entries": len(report["weather"]),
                "alerts_and_key_events": [e["text"] for e in report["events"]][-10:], "thermal": {k: v for k, v in report["thermal"].items() if k != "camera"},
                "post_burn": report["operation"]["post_burn"], "fingerprint": report["fingerprint"], "limitations": report["limitations"]}

    @add("propose_field", "propose", "Propose saving a field for burn monitoring from its corners in order around the field ([lat, lon] pairs the "
         "customer gave, read from a map app or marked with the drone's GPS). For drawing on the map instead, use show_screen with target flame.",
         {"name": string("Field name"), "corners": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                                                     "description": "Corners [[lat, lon], ...] in order"},
          "watch_strip_m": number("Width of the strip outside the boundary to watch for heat (default 100)"),
          "fireguard_m": number("Width of the fireguard inside the boundary (default 0)"), "crop": string("Crop residue, e.g. wheat stubble")},
         ["name", "corners"])
    def propose_field(args, turn):
        from .fields import FieldError, clean_field

        payload = {"name": untrusted(args["name"], 60), "boundary": args["corners"], "crop": untrusted(args.get("crop") or "", 40)}
        for key, source in (("watch_buffer_m", "watch_strip_m"), ("fireguard_m", "fireguard_m")):
            if args.get(source) is not None:
                payload[key] = args[source]
        try:
            preview = clean_field(payload, site_origin=None, taken=set(), actor="preview")
        except FieldError as error:
            raise ToolError(str(error)) from None
        from .fields import view

        shape = view(preview, None)
        return toolbox._propose(turn, "flame_field", payload, f"Save field: {payload['name']}",
                                f"{len(preview['boundary'])} corners, about {shape['area_ha']:g} ha, perimeter about {shape['perimeter_m']:g} m.",
                                ["The field appears on the Flame tab map", f"Watch strip {preview['watch_buffer_m']:g} m outside the boundary",
                                 "Check the outline on the map before planning a burn"])

    @add("propose_drone_configuration", "propose", "Propose switching a drone between its security configuration and its agricultural "
         "monitoring configuration. Only possible on the ground with no open inspection; agricultural monitoring also needs the accessory "
         "attached, commissioned and inspected.",
         {"drone_id": string("Drone id"), "configuration": string("Target", enum=["security", "agricultural_monitoring"])}, ["drone_id", "configuration"])
    def propose_drone_configuration(args, turn):
        view = flame().configuration_view(args["drone_id"])
        if view["configuration"] == args["configuration"]:
            raise ToolError(f"the drone is already in its {view['label'].lower()} configuration")
        if view["switch_blockers"]:
            raise ToolError("cannot switch now: " + "; ".join(b["detail"] for b in view["switch_blockers"]))
        from ..drone.accessories import CONFIGURATIONS

        return toolbox._propose(turn, "flame_configuration", dict(args), f"Switch the drone to {CONFIGURATIONS[args['configuration']].lower()}",
                                "Changes which requests the drone accepts; nothing flies.", flame()._configuration_effects(args["configuration"]) + ["Audited"])


def apply_proposal(service: Any, kind: str, params: dict[str, Any], actor: str) -> dict[str, Any]:
    flame = service.flame
    if kind == "flame_field":
        field = flame.save_field(None, params, actor)
        return {"ok": True, "message": f"Field '{field['name']}' saved ({field['area_ha']:g} ha). Check its outline on the Flame tab.",
                "data": {"field_id": field["id"]}}
    if kind == "flame_configuration":
        view = flame.set_configuration(params["drone_id"], params["configuration"], actor)
        return {"ok": True, "message": f"The drone is now in its {view['label'].lower()} configuration."}
    return {"ok": False, "message": f"unknown action {kind}"}


# ---- offline guide ------------------------------------------------------------------------------

# Strong words are about Flame Mode on their own; weak ones only once Flame Mode is in use ("field of view" is a camera question).
STRONG = re.compile(r"\b(burns?|burning|fires?|hot ?spots?|flame|stubble|crop residue|agricultur\w*)\b")
WEAK = re.compile(r"\b(fields?|accessor(y|ies)|farm|equipment|checklist|operation|crew|permits?|prescription)\b(?! of view)")


def respond(guide: Any, text: str, lower: str, turn: Any) -> str | None:
    """Answers for Flame Mode questions, or None when the question is not about it."""
    flame = getattr(guide.tools.service, "flame", None)
    if flame is None:
        return None
    enabled = flame.enabled()
    about_flame = bool(STRONG.search(lower)) or (enabled and bool(WEAK.search(lower)))
    sensors = "sensor" in lower and any(word in lower for word in ("working", "ok", "okay", "status", "all"))
    monitored = any(word in lower for word in ("monitored", "being watched", "areas being", "what is watched"))
    if not (about_flame or (enabled and (sensors or monitored))):
        return None
    if any(word in lower for word in ("map this field", "map my field", "map a field", "add a field", "draw", "boundary", "mark the field", "new field")):
        return _map_field(guide, text, turn)
    if sensors:
        return _sensors(guide, turn)
    if any(word in lower for word in ("report", "summary of the operation", "summarise", "summarize")):
        return _report(guide, turn)
    if any(word in lower for word in ("hotspot", "hot spot", "outside the expected", "outside the field", "outside the boundary", "escape", "spot fire", "heat")):
        return _hotspots(guide, turn)
    if any(word in lower for word in ("incomplete", "checklist", "equipment", "checks", "ready to", "can i burn", "can we burn", "safe to", "start the burn", "light")):
        return _checklist(guide, turn, asked_permission=any(w in lower for w in ("can i", "can we", "safe to", "ok to", "okay to", "should i", "start the burn")))
    if monitored or "field" in lower:
        return _overview(guide, turn)
    return _overview(guide, turn)


def _map_field(guide: Any, text: str, turn: Any) -> str:
    corners = [[float(lat), float(lon)] for lat, lon in _COORD.findall(text)]
    if len(corners) >= 3:
        name = "Field"
        named = re.search(r"(?:called|named)\s+([\w' -]{2,40})", text, re.IGNORECASE)
        if named:
            name = named.group(1).strip()
        result, error = guide.tools.call("propose_field", {"name": name, "corners": corners}, turn)
        if error:
            return f"I couldn't use those corners: {result['error']}"
        return (f"I've prepared the field from {len(corners)} corners. Check the summary and press **Confirm** to save it; then check the "
                "outline on the Flame tab map.")
    guide.tools.call("show_screen", {"target": "flame", "label": "Draw the field"}, turn)
    return ("Three ways to map a field:\n"
            "- **Draw it** on the Flame tab: click each corner in order on the map, then save.\n"
            "- **Mark corners with the drone**: carry or fly the drone to each corner and press *Add corner at drone position* "
            "(its GPS is usually within a few metres).\n"
            "- **Type the corners** here as latitude, longitude pairs in order around the field, e.g. `49.9012, -97.6034`.\n\n"
            "Then set the fireguard width, the watch strip outside the boundary, and mark nearby homes, roads and towns so smoke direction can be checked.")


def _sensors(guide: Any, turn: Any) -> str:
    result = guide._call("get_drone_sensor_status", turn)
    lines = []
    for drone in result["drones"]:
        failing = [f"{c['label']}: {c['detail']}" for c in drone["checks_not_passing"]]
        lines.append(f"**{drone['aircraft']}**: {len(drone['checks_passing'])} checks passing"
                     + (". Not passing: " + "; ".join(failing[:5]) if failing else ", none failing."))
    for accessory in result["accessories"]:
        health = accessory["health"]
        if accessory["reporting"]:
            temps = f"heat shield {health.get('shield_temperature_c')} °C, bay {health.get('bay_temperature_c')} °C"
            lines.append(f"**{accessory['name']}** ({accessory['support']}): {accessory['attachment']}, {temps}"
                         + (f"; faults: {', '.join(accessory['faults'])}" if accessory["faults"] else "; no faults reported") + ".")
        else:
            lines.append(f"**{accessory['name']}** ({accessory['support']}): {accessory['detail']}.")
    for watch in result["thermal_watch"]:
        lines.append(f"Thermal watch of {watch['operation']}: {watch['thermal']['detail']}.")
    if not lines:
        return "No drone is registered, so there are no drone sensors to check."
    return "\n".join(f"- {line}" for line in lines) + "\n\n" + result["note"]


def _overview(guide: Any, turn: Any) -> str:
    data = guide._call("get_flame_overview", turn)
    if not data["fields"]:
        guide.choices(turn, ["Help me map this field"])
        return ("No field is mapped yet. Flame Mode watches a field during a crop-residue burn with the drone's thermal camera. "
                "Start by mapping the field's boundary.")
    lines = []
    for field in data["fields"]:
        areas = "; ".join(f"{a['label']}: {a['detail']}" for a in field["monitored_areas"])
        lines.append(f"**{field['name']}** ({field['area_ha']:g} ha): {areas}. {field['geofence'].capitalize()}.")
    for drone in data["drones"]:
        lines.append(f"Drone **{drone['name']}** is in its {drone['configuration'].lower()} configuration"
                     + (f" (cannot switch now: {drone['cannot_switch_because'][0]})" if drone["cannot_switch_because"] else "") + ".")
    return "\n".join(f"- {line}" for line in lines) + "\n\nThe thermal camera only watches where the drone looks; the operation view shows how much was covered."


def _checklist(guide: Any, turn: Any, *, asked_permission: bool) -> str:
    try:
        data = guide._call("get_burn_checklist", turn)
    except LookupError:
        guide.tools.call("show_screen", {"target": "flame", "label": "Plan a burn"}, turn)
        return "There is no open burn operation. Create one for a mapped field on the Flame tab; its checklist then tracks permits, crew, equipment, weather and the drone."
    lines = [f"{item['label']}: {item['detail'] or item['state']}" for item in data["incomplete"]]
    text = data["statement"] + ("\n\nStill open:\n" + "\n".join(f"- {line}" for line in lines[:12]) if lines else "\n\nEvery checklist item is recorded.")
    if data["equipment_not_checked"]:
        text += "\n\nEquipment not checked: " + ", ".join(data["equipment_not_checked"]) + "."
    if asked_permission:
        text += "\n\nI can't tell you whether to light the field or whether it is safe: that is the burn boss's decision under the permit's conditions."
    return text


def _hotspots(guide: Any, turn: Any) -> str:
    try:
        data = guide._call("get_hotspots", turn, outside_only=True)
    except LookupError:
        return "There is no open burn operation, so there is no thermal watch to report on."
    counts = data["counts"]
    coverage = data["coverage"]
    seen = (f"In the last {coverage['window_min']:g} minutes the camera covered {coverage['edge_pct']:g}% of the boundary edge and "
            f"{coverage['strip_pct']:g}% of the watch strip.") if coverage.get("available") else "No thermal coverage figures are available."
    if counts.get("outside") or counts.get("sensitive"):
        latest = [row["text"] for row in data["latest"] if row["where"] in ("outside", "sensitive")][-3:]
        return ("**Yes. Heat was observed outside the expected area:**\n" + "\n".join(f"- {t}" for t in latest)
                + f"\n\n{seen} Check these places from the ground now; if a fire has escaped, call 911.")
    edge = counts.get("edge", 0)
    text = "No heat was observed outside the field boundary" + (f"; {edge} observation(s) of heat at the boundary edge." if edge else ".")
    return f"{text} {seen} {data['note']}" + (f" Thermal watch: {data['thermal']['detail']}." if data["thermal"].get("state") != "observing" else "")


def _report(guide: Any, turn: Any) -> str:
    try:
        data = guide._call("get_operation_report", turn)
    except LookupError:
        return "No burn operation has been recorded yet."
    thermal = data["thermal"]
    return (f"Report for **{data['operation']}** ({data['status']}{', simulated' if data['simulated'] else ''}): {data['checklist']} "
            f"{data['weather_entries']} weather observation(s); heat outside the boundary {thermal['heat_outside_boundary']} time(s), at the boundary "
            f"{thermal['heat_at_boundary']}, remaining after the burn {thermal['remaining_heat_post_burn']}. Post-burn result: "
            f"{(data['post_burn'] or {}).get('result', 'not recorded yet')}. The printable report has every entry and its fingerprint "
            f"({data['fingerprint'][:12]}…).")


__all__ = ["LIMITS", "SCREEN", "apply_proposal", "register_tools", "respond"]
