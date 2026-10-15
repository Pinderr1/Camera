"""End-to-end drone inspection demo against ArduPilot SITL.

    security incident (fixed camera) -> Sentinel inspection request -> operator
    approval -> pilot flies Sentinel's route (SITL) -> drone telemetry and video
    -> observations and evidence attached to the incident

Everything runs through the real service and API: the simulated backyard
camera feeds the normal pipeline (scripted detector), the drone is real
ArduCopter firmware in SITL reached through the companion relay (telemetry
policy: the Hub cannot command it), the companion agent pairs and reports over
the API, and the drone camera is simulated from SITL's live position.

Start SITL first (bash scripts/sitl/start_sitl.sh 3), then:

    python scripts/drone_sitl_demo.py --out bench_runs/drone_demo

Drone models: `--model sentinel_pro` uses the same companion path with the
Pro's checks. `--model sentinel_lite` needs SITL started with the Lite
commissioning (SITL_PARAMS=config/drone/sentinel-lite-sitl.parm): no
companion, the flight controller's own command lockout, and one shared radio
(emulated by Sentinel's relay in pass-through mode) carrying the Hub's and the
pilot's MAVLink; Sentinel audits the parameters and fence the pilot's ground
station downloads.

What is simulated: the aircraft (SITL physics, real firmware), both cameras,
and the pilot (scripts/sitl/gcs_standin.py)."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "sitl"))

HUB_UDP = 14551
PILOT_UDP = 14561
MODELS = ("generic_ardupilot", "sentinel_lite", "sentinel_pro")


def demo_config(storage: Path, model: str = "generic_ardupilot") -> dict[str, Any]:
    config = json.loads((ROOT / "config" / "sentinel.example.json").read_text())
    config = copy.deepcopy(config)
    config["_config_dir"] = str(ROOT / "config")
    config["storage"] = config.get("storage", {}) | {"dir": str(storage)}
    backyard = next(camera for camera in config["cameras"] if camera["id"] == "cam_backyard")
    backyard["source"] = {"type": "simulated", "scenario": "scenarios/drone_shed.json"}
    config["cameras"] = [backyard]
    config["topology"] = {"links": []}
    zones = {zone["id"] for zone in backyard["zones"]}
    config["rules"] = [rule for rule in config["rules"] if set(rule.get("zones", [])) <= zones and rule["type"] != "sequence"]
    config["detector"] = config["detector"] | {"type": "scripted"}
    config["mqtt"] = {"enabled": False}
    drone = config["drone"]
    drone.update({
        "adapter": "mavlink", "connection": f"udpin:127.0.0.1:{HUB_UDP}", "auto_request_min_severity": "high",
        "safety": drone.get("safety", {}) | {"inspection_altitude_m": 12, "hover_s": 8},
        "firmware_tested": ["4.6", "4.7"],
        "video": {
            "source": {"type": "simulated_aerial", "objects": [
                {"label": "person", "path": [{"t": 0, "map": [34.6, 4.0]}, {"t": 20, "map": [35.4, 4.4]}, {"t": 40, "map": [34.6, 4.0]}]}]},
            "camera": {"hfov_deg": 80, "vfov_deg": 50, "default_pitch_deg": -90}, "fps": 5,
        },
    })
    if model != "generic_ardupilot":
        drone["drone_model"] = model
        drone["video"].pop("camera")  # the model's camera geometry applies
    return config


def wait(predicate: Callable[[], Any], timeout: float, what: str, log: Callable[[str], None]) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            log(f"  ok: {what}")
            return value
        time.sleep(0.5)
    raise TimeoutError(what)


def run_demo(out: Path, *, hub_sitl: str = "tcp:127.0.0.1:5762", gcs_sitl: str = "tcp:127.0.0.1:5763", model: str = "generic_ardupilot",
             log: Callable[[str], None] = print) -> dict[str, Any]:
    from fastapi.testclient import TestClient
    from gcs_standin import SitlGroundStation

    from sentinel.api import create_app
    from sentinel.companion.agent import CompanionAgent, pair
    from sentinel.companion.relay import MavlinkRelay
    from sentinel.config import validate_config
    from sentinel.service import SentinelService

    out.mkdir(parents=True, exist_ok=True)
    storage = Path(tempfile.mkdtemp(prefix="sentinel-drone-demo-"))
    config = demo_config(storage, model)
    lite = model == "sentinel_lite"
    validate_config({k: v for k, v in config.items() if not k.startswith("_")})
    timeline: list[dict[str, Any]] = []

    def mark(step: str, **details: Any) -> None:
        timeline.append({"t_ms": int(time.time() * 1000), "step": step} | details)
        log(f"[{len(timeline):2d}] {step}" + (f" {details}" if details else ""))

    if lite:
        # Sentinel Lite: no companion. One HM30 radio carries the Hub's and the pilot's MAVLink.
        relay = MavlinkRelay(hub_sitl, f"udp:127.0.0.1:{HUB_UDP}", [f"udp:127.0.0.1:{PILOT_UDP}"], policy="sitl").start()
        mark("radio link (HM30 stand-in) started", fc=hub_sitl)
    else:
        relay = MavlinkRelay(hub_sitl, f"udp:127.0.0.1:{HUB_UDP}", policy="telemetry").start()
        mark("companion relay started", fc=hub_sitl, policy="telemetry")
    service = SentinelService(config, start_cameras=True)
    service.runtime.update_settings({"arm_mode": "away"}, "demo")
    service.start()
    api = TestClient(create_app(service, api_token=None))
    gcs = SitlGroundStation(f"udpin:127.0.0.1:{PILOT_UDP}" if lite else gcs_sitl)
    agent = None
    try:
        drone = service.drone
        mark("service started", drone_mode=drone.mode()[0], adapter=drone.adapter.kind, model=drone.model["name"])
        if lite:
            if gcs.get_param("MAV_OPTIONS") != 1:
                raise SystemExit("start SITL with SITL_PARAMS=config/drone/sentinel-lite-sitl.parm for --model sentinel_lite")
            gcs.start_heartbeats()
            run_lite = True
        else:
            run_lite = False

        code = api.post("/api/v1/drone/companions/code", json={}).json() if not run_lite else None
        identity_path = storage / "companion-identity.json"
        hub_url = str(api.base_url)

        def post(url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
            headers = {"authorization": f"Companion {token}"} if token else {}
            response = api.post(url.replace(hub_url, ""), json=body, headers=headers)
            if response.status_code >= 400:
                from sentinel.companion.agent import HubError

                raise HubError(response.status_code, response.json().get("detail", ""))
            return response.json()

        import sentinel.companion.agent as agent_module

        if not run_lite:
            original_post = agent_module._post
            agent_module._post = post
            try:
                paired = pair(hub_url, code["code"], "SITL companion", identity_path)
            finally:
                agent_module._post = original_post
            identity = json.loads(identity_path.read_text())
            agent = CompanionAgent(identity, relay_status=relay.status, post=post, interval_s=1.0).start()
            wait(lambda: (drone.companion_status() or {}).get("state") == "reporting", 20, "companion paired and reporting", log)
            mark("companion paired", companion_id=paired["companion_id"])
        else:
            wait(lambda: drone.adapter.telemetry().link_ok, 30, "Hub hears the aircraft over the radio", log)
            count = gcs.download_parameters()
            mark("pilot's ground station connected and downloaded the parameters; Sentinel overheard them", parameters=count)

        wait(lambda: drone.adapter.params_complete() and drone.adapter.telemetry().gps_fix >= 3, 90, "telemetry and parameters", log)
        incident = wait(lambda: next((i for i in api.get("/api/v1/incidents").json() if i["severity"] == "high"), None), 90, "incident from the fixed camera", log)
        mark("incident opened", incident_id=incident["incident_id"], title=incident["title"], severity=incident["severity"])
        request = wait(lambda: next((r for r in api.get("/api/v1/drone").json()["requests"] if r["incident_id"] == incident["incident_id"]), None),
                       15, "inspection requested by Sentinel", log)
        mark("inspection requested", request_id=request["request_id"], target=request["target_label"], reason=request["reason"])
        request_id = request["request_id"]

        preview = api.get(f"/api/v1/drone/inspections/{request_id}/mission").json()
        fence_vertices = gcs.load_fence(preview)
        drone.adapter.refresh()
        if run_lite:
            gcs.download_fence()  # the ground station reads it back; Sentinel overhears it
        mark("commissioning engineer loaded the fence from Sentinel's export", vertices=fence_vertices)
        gcs.swap_battery()
        api.post("/api/v1/drone/conditions", json={"precipitation": False, "visibility_ok": True, "wind_mps": 3, "temperature_c": 14, "daylight": True})
        wait(lambda: drone.parameter_audit()["ok"], 60, "parameter audit passes", log)
        commissioning = api.post("/api/v1/drone/maintenance", json={"kind": "commissioning", "note": "SITL demo commissioning"}).json()
        mark("commissioning recorded", fingerprint=commissioning.get("fingerprint"), firmware=commissioning.get("firmware"))
        readiness = wait(lambda: (lambda r: r if r["ready"] else None)(api.get("/api/v1/drone/readiness").json()), 60, "ready for approval", log)
        mark("readiness", checks={c["id"]: c["state"] for c in readiness["checks"]})
        lockout = next((c for c in readiness["checks"] if c["id"] == "lockout"), None)
        if lockout:
            mark("Hub command lockout", state=lockout["state"], detail=lockout["detail"])

        approved = api.post(f"/api/v1/drone/inspections/{request_id}/approve", headers={"x-sentinel-operator": "demo-operator"})
        approved.raise_for_status()
        mark("operator approved", status=approved.json()["status"], mode=approved.json()["mode"])
        plan_response = api.get(f"/api/v1/drone/inspections/{request_id}/mission")
        (out / "inspection.plan").write_text(plan_response.text, encoding="utf-8")
        (out / "inspection.waypoints").write_text(api.get(f"/api/v1/drone/inspections/{request_id}/mission?format=waypoints").text, encoding="utf-8")
        plan = plan_response.json()
        gcs.load_mission(plan)
        gcs.start_mission()
        mark("pilot loaded the plan and started the mission", items=len(plan["mission"]["items"]))

        wait(lambda: api.get(f"/api/v1/drone/inspections/{request_id}").json()["phase"] == "inspecting", 120, "drone at the inspection point", log)
        mark("drone over the inspection point", telemetry={k: drone.adapter.telemetry().as_dict()[k] for k in ("alt_m", "flight_mode", "battery_pct")})
        done = wait(lambda: (lambda r: r if r["status"] in ("completed", "aborted") else None)(api.get(f"/api/v1/drone/inspections/{request_id}").json()),
                    240, "inspection finished and aircraft landed", log)
        final = wait(lambda: (lambda r: r if r["observations"].get("final") else None)(api.get(f"/api/v1/drone/inspections/{request_id}").json()),
                     20, "final drone observations", log)
        mark("inspection finished", status=done["status"], note=done["note"], frames=final["observations"].get("frames"))

        explanation = api.get(f"/api/v1/incidents/{incident['incident_id']}/explanation").json()
        drone_section = explanation.get("drone_inspections", [])
        evidence = [item for item in explanation["evidence"] if item["type"] == "drone_snapshot"]
        hashes = service.runtime.store.evidence_hashes([item["path"] for item in evidence])
        flights = api.get("/api/v1/drone/flights").json()
        audit_actions = [row["action"] for row in service.runtime.store.list_audit(500)]
        report = {
            "incident": {k: incident[k] for k in ("incident_id", "title", "severity")},
            "inspection": {k: final[k] for k in ("request_id", "status", "mode", "decided_by", "reason", "note", "flight_id")},
            "observations": final["observations"],
            "explanation_drone_section": drone_section,
            "evidence": [item | {"sha256": hashes.get(item["path"], {}).get("sha256"), "exists": (service.runtime.media_root / item["path"]).exists()}
                         for item in evidence],
            "flight": flights[0] if flights else None,
            "model": drone.status()["model"],
            "lockout": lockout,
            "relay": relay.status(),
            "companion": drone.companion_status(),
            "audit": [action for action in audit_actions if action.startswith("drone_")],
            "timeline": timeline,
        }
        for stale in out.glob("*.jpg"):
            stale.unlink()
        (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out / "report.md").write_text(render(report), encoding="utf-8")
        for item in evidence[:3]:
            source = service.runtime.media_root / item["path"]
            if source.exists():
                (out / Path(item["path"]).name).write_bytes(source.read_bytes())
        return report
    finally:
        if agent:
            agent.stop()
        gcs.close()
        service.stop()
        relay.stop()


def render(report: dict[str, Any]) -> str:
    observations = report["observations"]
    flight = report["flight"] or {}
    lines = [
        f"# Drone inspection demo (ArduPilot SITL): {report['model']['name']}", "",
        "Simulated: the aircraft (ArduCopter firmware in SITL), the fixed camera, the drone camera, the pilot and the radio link.", "",
        f"- Hub command lockout: {(report.get('lockout') or {}).get('detail', 'not checked')}",
        f"- Incident: {report['incident']['title']} ({report['incident']['severity']}), `{report['incident']['incident_id']}`",
        f"- Inspection: `{report['inspection']['request_id']}`, mode {report['inspection']['mode']}, status **{report['inspection']['status']}**, "
        f"approved by {report['inspection']['decided_by']}",
        f"- Flight: {flight.get('duration_s')} s, max altitude {flight.get('max_alt_m')} m, {flight.get('distance_m')} m flown, "
        f"battery {flight.get('battery_start_pct')}% to {flight.get('battery_end_pct')}%",
        f"- Drone video: {observations.get('frames')} frames analysed, {observations.get('frames_with_pose')} with a camera position",
        (f"- Radio (pass-through): {report['relay']['hub_frames_forwarded']} Hub frames reached the flight controller, which ignores them (command lockout)"
         if report['relay']['policy'] == 'sitl' else
         f"- Relay: {report['relay']['hub_frames_forwarded']} Hub frames forwarded, {report['relay']['hub_frames_dropped']} dropped"),
        f"- Evidence: {len(report['evidence'])} drone snapshot(s), fingerprinted: {all(e['sha256'] for e in report['evidence'])}", "",
        "## What the drone observed", "",
    ] + [f"> {line}" for line in observations.get("text", [])] + ["", "## Timeline", ""]
    lines += [f"{i + 1}. {step['step']}" for i, step in enumerate(report["timeline"])]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="bench_runs/drone_demo")
    parser.add_argument("--hub-sitl", default="tcp:127.0.0.1:5762")
    parser.add_argument("--gcs-sitl", default="tcp:127.0.0.1:5763")
    parser.add_argument("--model", choices=MODELS, default="generic_ardupilot", help="drone model whose checks and camera apply")
    args = parser.parse_args()
    report = run_demo(Path(args.out), hub_sitl=args.hub_sitl, gcs_sitl=args.gcs_sitl, model=args.model)
    print(render(report))
    return 0 if report["inspection"]["status"] == "completed" and report["evidence"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
