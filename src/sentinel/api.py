"""HTTP API (versioned under /api/v1) and dashboard host.

Designed for the bundled dashboard and a future mobile app alike: JSON
resources, an SSE live stream, MJPEG previews, and bearer-token auth (the
dashboard trades the token for an HttpOnly cookie so <img> and EventSource
work without putting secrets in URLs)."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backup, devices, netdiag, onboarding, onvif, planner
from .config import public_camera_view
from .drone import FlightCommandRejected
from .drone.pairing import PairingError
from .installation import InstallationError
from .service import SentinelService
from .site_config import WRITE_ONLY_FIELDS, SiteConfigError

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "sentinel_session"


class SessionBody(BaseModel):
    token: str


class SettingsBody(BaseModel):
    arm_mode: Literal["disarmed", "home", "away", "night", "vacation"] | None = None
    sensitivity: Literal["low", "normal", "high"] | None = None
    notifications_enabled: bool | None = None


class ResolveBody(BaseModel):
    outcome: Literal["expected_activity", "false_alarm", "real_concern", "unclear"]


class InspectionBody(BaseModel):
    zone_id: str | None = None
    point_id: str | None = Field(default=None, max_length=60)  # one of drone.inspection_points
    lat: float | None = None
    lon: float | None = None
    incident_id: str = ""


class DenyBody(BaseModel):
    note: str = Field(default="", max_length=500)


class DiagnoseBody(BaseModel):
    host: str = Field(max_length=253)
    brand: str | None = Field(default=None, max_length=20)
    device_mask: str | None = Field(default=None, max_length=15)
    device_gateway: str | None = Field(default=None, max_length=15)
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    http_port: int = Field(default=80, ge=1, le=65535)
    discover: bool = True


class DiscoverBody(BaseModel):
    sadp: bool = True
    onvif: bool = True
    port_check: bool = False
    confirm: bool = False  # the person confirmed they own or manage this network


class ChannelsBody(BaseModel):
    source: dict[str, Any] | None = None
    draft_id: str | None = None
    max_channels: int = Field(default=16, ge=1, le=64)


class ProtectBody(BaseModel):
    protected: bool = True


class BackupBody(BaseModel):
    label: str = Field(default="manual", max_length=24, pattern=r"^[a-z0-9_-]*$")


class RestoreBody(BaseModel):
    parts: list[Literal["config", "settings", "database"]] = Field(default_factory=lambda: ["config", "settings"])


class RuleEnabledBody(BaseModel):
    enabled: bool


class WalkStep(BaseModel):
    zone_id: str = Field(max_length=80)
    stay_s: float = Field(default=10, ge=1, le=3600)
    gap_s: float = Field(default=5, ge=0.5, le=600)


class RuleTestBody(BaseModel):
    rule: dict[str, Any]
    source: Literal["walk", "scenario", "history"] = "walk"
    arm_mode: Literal["disarmed", "home", "away", "night", "vacation"] = "away"
    start: str | None = Field(default=None, max_length=32)  # ISO local time for walk/scenario
    steps: list[WalkStep] = Field(default_factory=list, max_length=12)
    label: Literal["person", "vehicle", "bicycle", "animal"] = "person"
    scenario: str | None = Field(default=None, max_length=60)
    days: float = Field(default=7, gt=0, le=14)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class MissedBody(BaseModel):
    occurred_ms: int
    camera_id: str = Field(max_length=80)
    zone_id: str = Field(default="", max_length=80)
    category: Literal["person", "vehicle", "bicycle", "animal", "other", ""] = ""
    description: str = Field(default="", max_length=1000)


class PropertyBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    property_type: Literal["house", "corner_lot", "townhouse", "storefront", "warehouse", "office", "other"] = "house"
    width_m: float = Field(ge=1, le=5000)
    height_m: float = Field(ge=1, le=5000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    notes: str = Field(default="", max_length=500)


class CompleteBody(BaseModel):
    acknowledge_issues: bool = False


class ResetBody(BaseModel):
    keep_plan: bool = True


class DroneCommandBody(BaseModel):
    action: Literal["takeoff", "goto", "hold", "return_home", "land"]
    params: dict[str, float] = Field(default_factory=dict)


class ConditionsBody(BaseModel):
    precipitation: bool
    visibility_ok: bool
    wind_mps: float | None = Field(default=None, ge=0, le=60)
    temperature_c: float | None = Field(default=None, ge=-60, le=60)
    note: str = Field(default="", max_length=300)


class MaintenanceBody(BaseModel):
    kind: Literal["inspection", "repair", "battery", "propellers", "firmware", "calibration", "commissioning", "note"]
    note: str = Field(default="", max_length=1000)


class PairingCodeBody(BaseModel):
    drone_id: str | None = Field(default=None, max_length=80)


class CompanionPairBody(BaseModel):
    code: str = Field(min_length=6, max_length=16)
    name: str = Field(default="", max_length=60)
    hardware: dict[str, Any] = Field(default_factory=dict)


class CompanionSessionBody(BaseModel):
    companion_id: str = Field(max_length=40)
    timestamp_ms: int
    nonce: str = Field(min_length=8, max_length=64)
    mac: str = Field(min_length=64, max_length=64)


class CompanionReportBody(BaseModel):
    health: dict[str, Any] = Field(default_factory=dict)
    relay: dict[str, Any] = Field(default_factory=dict)
    video: dict[str, Any] = Field(default_factory=dict)
    sent_ms: int | None = None


def create_app(service: SentinelService, *, api_token: str | None) -> FastAPI:
    runtime = service.runtime
    app = FastAPI(title="Sentinel API", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def require_auth(request: Request) -> str:
        if api_token:
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
            if not supplied or not hmac.compare_digest(supplied, api_token):
                raise HTTPException(status_code=401, detail="authentication required")
        return request.headers.get("x-sentinel-operator", "operator")[:64]

    def camera_or_404(camera_id: str) -> dict[str, Any]:
        camera = runtime.camera_configs.get(camera_id)
        if camera is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        return camera

    def drone_or_404():
        if service.drone is None:
            raise HTTPException(status_code=404, detail="drone integration disabled")
        return service.drone

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "time_ms": int(time.time() * 1000)}

    @app.post("/api/v1/session")
    def create_session(body: SessionBody, request: Request, response: Response) -> dict[str, bool]:
        if api_token and not hmac.compare_digest(body.token, api_token):
            raise HTTPException(status_code=401, detail="invalid token")
        response.set_cookie(
            COOKIE, body.token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400
        )
        return {"ok": True, "auth_required": bool(api_token)}

    @app.delete("/api/v1/session")
    def end_session(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/v1/site")
    def site(_: str = Depends(require_auth)) -> dict[str, Any]:
        config = service.config
        return {
            "site": config["site"],
            "cameras": [public_camera_view(camera) for camera in runtime.camera_configs.values()],
            "topology": config.get("topology", {}),
            "rules": config.get("rules", []),
            "privacy": config.get("privacy", {}),
            "drone_enabled": service.drone is not None,
            "drone_points": config.get("drone", {}).get("inspection_points", []) if service.drone is not None else [],
        }

    @app.get("/api/v1/state")
    def state(_: str = Depends(require_auth)) -> dict[str, Any]:
        return runtime.snapshot_state()

    @app.get("/api/v1/cameras")
    def list_cameras(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return [
            public_camera_view(camera) | {"status": runtime.camera_status.get(camera_id, {})}
            for camera_id, camera in runtime.camera_configs.items()
        ]

    @app.get("/api/v1/cameras/{camera_id}/snapshot.jpg")
    def snapshot(camera_id: str, _: str = Depends(require_auth)) -> Response:
        camera_or_404(camera_id)
        jpeg = runtime.latest_jpeg.get(camera_id)
        if not jpeg:
            raise HTTPException(status_code=503, detail="no frame yet")
        # The last frame stays available after a camera stops; clients must not mistake it for live video.
        status = runtime.camera_status.get(camera_id, {})
        age_ms = max(0, int(time.time() * 1000) - int(status.get("last_frame_ms") or 0))
        monitoring = "true" if status.get("health", {}).get("monitoring") else "false"
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Frame-Age-Ms": str(age_ms), "X-Sentinel-Monitoring": monitoring})

    @app.get("/api/v1/cameras/{camera_id}/stream.mjpg")
    async def stream(camera_id: str, request: Request, _: str = Depends(require_auth)) -> StreamingResponse:
        camera = camera_or_404(camera_id)
        interval = 1.0 / float(camera.get("fps", 5))

        async def frames():
            last = None
            while not await request.is_disconnected():
                jpeg = runtime.latest_jpeg.get(camera_id)
                if jpeg and jpeg is not last:
                    last = jpeg
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(interval)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/events")
    def events(
        since_ms: int | None = None,
        until_ms: int | None = None,
        camera_id: str | None = None,
        min_severity: Literal["info", "low", "medium", "high"] | None = None,
        incident_id: str | None = None,
        limit: int = 200,
        _: str = Depends(require_auth),
    ) -> list[dict[str, Any]]:
        return runtime.store.list_events(
            since_ms=since_ms, until_ms=until_ms, camera_id=camera_id, min_severity=min_severity, incident_id=incident_id, limit=limit
        )

    @app.get("/api/v1/incidents")
    def incidents(status: Literal["open", "closed"] | None = None, since_ms: int | None = None, limit: int = 100, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return runtime.store.list_incidents(status=status, since_ms=since_ms, limit=limit)

    @app.get("/api/v1/incidents/{incident_id}")
    def incident(incident_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        found = runtime.store.get_incident(incident_id)
        if found is None:
            raise HTTPException(status_code=404, detail="unknown incident")
        return found | {"events": runtime.store.list_events(incident_id=incident_id, limit=500)}

    @app.post("/api/v1/incidents/{incident_id}/acknowledge")
    def acknowledge(incident_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return runtime.acknowledge(incident_id, actor)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident") from None

    @app.post("/api/v1/incidents/{incident_id}/resolve")
    def resolve(incident_id: str, body: ResolveBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return runtime.resolve(incident_id, actor, body.outcome)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident") from None

    @app.post("/api/v1/incidents/{incident_id}/protect")
    def protect(incident_id: str, body: ProtectBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return runtime.protect(incident_id, actor, body.protected)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident") from None

    # ---- investigation ------------------------------------------------------------

    @app.get("/api/v1/search/incidents")
    def search_incidents(
        since_ms: int | None = None, until_ms: int | None = None, camera_id: str | None = None, zone_id: str | None = None,
        label: str | None = None, min_severity: Literal["info", "low", "medium", "high"] | None = None,
        outcome: Literal["expected_activity", "false_alarm", "real_concern", "unclear", "unreviewed"] | None = None,
        rule_id: str | None = None, q: str | None = None, limit: int = 200, _: str = Depends(require_auth),
    ) -> list[dict[str, Any]]:
        return runtime.store.search_incidents(since_ms=since_ms, until_ms=until_ms, camera_id=camera_id, zone_id=zone_id, label=label,
                                              min_severity=min_severity, outcome=outcome, rule_id=rule_id, text=(q or "")[:100] or None, limit=limit)

    @app.get("/api/v1/incidents/{incident_id}/explanation")
    def incident_explanation(incident_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return service.explain(incident_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident") from None

    @app.post("/api/v1/incidents/{incident_id}/notes")
    def add_note(incident_id: str, body: NoteBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return runtime.add_note(incident_id, actor, body.text)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/incidents/{incident_id}/export")
    def export_incident(incident_id: str, include_media: bool = False, actor: str = Depends(require_auth)) -> Response:
        try:
            data = service.export_incident(incident_id, actor, include_media=include_media)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown incident") from None
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="sentinel-{incident_id}.zip"', "Cache-Control": "no-store"})

    @app.post("/api/v1/missed")
    def report_missed(body: MissedBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return runtime.report_missed(actor, occurred_ms=body.occurred_ms, camera_id=body.camera_id, zone_id=body.zone_id,
                                         category=body.category, description=body.description)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown camera") from None

    @app.get("/api/v1/missed")
    def missed_reports(days: float = 30, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return runtime.store.list_missed_reports(since_ms=int(time.time() * 1000 - days * 86_400_000))

    @app.get("/api/v1/reports/performance")
    def performance(days: float = 30, _: str = Depends(require_auth)) -> dict[str, Any]:
        from .reports import performance_report

        return performance_report(runtime.store, service.config, days=max(1.0, min(days, 365.0)))

    @app.get("/api/v1/insights")
    def insights(days: float | None = None, _: str = Depends(require_auth)) -> dict[str, Any]:
        return service.insights(days)

    # ---- security rules ---------------------------------------------------------------

    @app.get("/api/v1/rules")
    def list_rules(_: str = Depends(require_auth)) -> dict[str, Any]:
        return service.rules_view()

    @app.post("/api/v1/rules")
    def create_rule(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.save_rule, None, body, actor)

    @app.put("/api/v1/rules/{rule_id}")
    def update_rule(rule_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.save_rule, rule_id, body, actor)

    @app.delete("/api/v1/rules/{rule_id}")
    def delete_rule(rule_id: str, actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.delete_rule, rule_id, actor)
        return {"ok": True}

    @app.put("/api/v1/rules/{rule_id}/enabled")
    def enable_rule(rule_id: str, body: RuleEnabledBody, actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.set_rule_enabled, rule_id, body.enabled, actor)
        return {"ok": True}

    @app.post("/api/v1/rules/check")
    def check_rule(body: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
        """Validate a draft rule and describe it in plain language, without saving."""
        return service.check_rule(body)

    @app.post("/api/v1/rules/test")
    def test_rule(body: RuleTestBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            result = service.test_rule(body.model_dump())
        except (ValueError, SiteConfigError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        runtime.store.audit(actor, "rule_tested", {"rule_id": body.rule.get("id", ""), "source": body.source, "fired": len(result["fired"])})
        return result

    # ---- system health, outages, backups ----------------------------------------

    @app.get("/api/v1/system/health")
    def system_health(_: str = Depends(require_auth)) -> dict[str, Any]:
        return service.health_report()

    @app.get("/api/v1/system/outages")
    def outages(since_ms: int | None = None, camera_id: str | None = None, limit: int = 200, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return runtime.store.list_outages(since_ms=since_ms, camera_id=camera_id, limit=limit)

    @app.post("/api/v1/cameras/{camera_id}/restart")
    def restart_camera(camera_id: str, actor: str = Depends(require_auth)) -> dict[str, bool]:
        camera_or_404(camera_id)
        try:
            service.restart_camera(camera_id, actor)
        except KeyError:
            raise HTTPException(status_code=409, detail="camera processing is not running in this session") from None
        return {"ok": True}

    @app.get("/api/v1/system/backups")
    def list_backups(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return backup.list_backups(service.storage_dir)

    @app.post("/api/v1/system/backups")
    def create_backup(body: BackupBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return service.create_backup(label=body.label or "manual", actor=actor)
        except OSError as error:
            raise HTTPException(status_code=507, detail=f"backup could not be written: {error.strerror or error}") from None

    @app.post("/api/v1/system/backups/{name}/verify")
    def verify_backup(name: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            result = backup.verify_backup(backup.resolve_backup(service.storage_dir, name))
        except backup.BackupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        return {"ok": result["ok"], "problems": result["problems"]}

    @app.post("/api/v1/system/backups/{name}/restore")
    def restore_backup(name: str, body: RestoreBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return service.restore_backup(name, list(body.parts), actor)
        except backup.BackupError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except SiteConfigError as error:
            raise HTTPException(status_code=422, detail=f"the backup's configuration was rejected: {error}") from None

    @app.get("/api/v1/media/{media_path:path}")
    def media(media_path: str, _: str = Depends(require_auth)) -> FileResponse:
        root = runtime.media_root.resolve()
        target = (root / media_path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target)

    @app.get("/api/v1/settings")
    def get_settings(_: str = Depends(require_auth)) -> dict[str, Any]:
        return dict(runtime.settings)

    @app.patch("/api/v1/settings")
    def patch_settings(body: SettingsBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        try:
            return runtime.update_settings(changes, actor)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/audit")
    def audit(limit: int = 100, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return runtime.store.list_audit(limit=min(limit, 500))

    @app.get("/api/v1/stream")
    async def live(request: Request, _: str = Depends(require_auth)) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)

        def forward(topic: str, payload: dict[str, Any]) -> None:
            def put() -> None:
                if not queue.full():
                    queue.put_nowait((topic, payload))

            loop.call_soon_threadsafe(put)

        async def messages():
            unsubscribe = runtime.bus.subscribe(forward)
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        topic, payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {topic}\ndata: {json.dumps(payload, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                unsubscribe()

        return StreamingResponse(messages(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/drone")
    def drone_status(_: str = Depends(require_auth)) -> dict[str, Any]:
        return drone_or_404().status()

    @app.post("/api/v1/drone/inspections")
    def request_inspection(body: InspectionBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        drone = drone_or_404()
        try:
            if body.zone_id:
                zone = runtime.zones.get(body.zone_id)
                if zone is None or "map_polygon" not in zone:
                    raise HTTPException(status_code=422, detail="zone has no map position")
                polygon = zone["map_polygon"]
                centroid = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
                request = drone.request_inspection(requested_by=actor, map_point=centroid, label=zone.get("name", zone["id"]), incident_id=body.incident_id)
            elif body.point_id:
                point = next((p for p in service.config.get("drone", {}).get("inspection_points", []) if p["id"] == body.point_id), None)
                if point is None:
                    raise HTTPException(status_code=404, detail="unknown inspection point")
                request = drone.request_inspection(requested_by=actor, map_point=tuple(point["map"]), label=point["name"], incident_id=body.incident_id)
            else:
                request = drone.request_inspection(requested_by=actor, lat=body.lat, lon=body.lon, label="custom point", incident_id=body.incident_id)
        except (FlightCommandRejected, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return request.as_dict()

    @app.get("/api/v1/drone/inspections/{request_id}/route")
    def drone_route(request_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        drone = drone_or_404()
        request = drone.requests.get(request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="unknown request")
        return request.route or drone.plan(request).as_dict() | {"preview": True}

    @app.post("/api/v1/drone/inspections/{request_id}/approve")
    def approve(request_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return drone_or_404().approve(request_id, actor).as_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown request") from None
        except FlightCommandRejected as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/v1/drone/inspections/{request_id}/deny")
    def deny(request_id: str, body: DenyBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return drone_or_404().deny(request_id, actor, body.note).as_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown request") from None
        except FlightCommandRejected as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/v1/drone/commands")
    def drone_command(body: DroneCommandBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        drone = drone_or_404()
        try:
            drone.command(body.action, actor, body.params)
        except (FlightCommandRejected, ValueError, KeyError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return drone.status()

    @app.get("/api/v1/drone/readiness")
    def drone_readiness(_: str = Depends(require_auth)) -> dict[str, Any]:
        return drone_or_404().readiness()

    @app.get("/api/v1/drone/link")
    def drone_link(_: str = Depends(require_auth)) -> dict[str, Any]:
        adapter = drone_or_404().adapter
        return adapter.link_status() if hasattr(adapter, "link_status") else {"state": "simulated", "connection": "built-in simulator"}

    @app.post("/api/v1/drone/conditions")
    def drone_conditions(body: ConditionsBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return drone_or_404().set_conditions(actor, precipitation=body.precipitation, visibility_ok=body.visibility_ok,
                                             wind_mps=body.wind_mps, temperature_c=body.temperature_c, note=body.note)

    @app.get("/api/v1/drone/inspections")
    def drone_inspections(incident_id: str | None = None, limit: int = 50, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        drone_or_404()
        return runtime.store.list_drone_inspections(incident_id=incident_id, limit=max(1, min(limit, 500)))

    @app.get("/api/v1/drone/inspections/{request_id}")
    def drone_inspection(request_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        found = drone_or_404().inspection(request_id)
        if found is None:
            raise HTTPException(status_code=404, detail="unknown request")
        return found

    @app.get("/api/v1/drone/inspections/{request_id}/mission")
    def drone_mission(request_id: str, format: Literal["plan", "waypoints"] = "plan", actor: str = Depends(require_auth)) -> Response:
        try:
            filename, content = drone_or_404().mission_export(request_id, format)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown request") from None
        runtime.store.audit(actor, "drone_mission_exported", {"request_id": request_id, "format": format})
        media_type = "application/json" if format == "plan" else "text/plain"
        return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})

    @app.get("/api/v1/drone/flights")
    def drone_flights(limit: int = 50, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return drone_or_404().flights(max(1, min(limit, 500)))

    @app.get("/api/v1/drone/maintenance")
    def drone_maintenance(_: str = Depends(require_auth)) -> dict[str, Any]:
        drone = drone_or_404()
        return {"summary": drone.maintenance_summary(), "records": drone.maintenance()}

    @app.post("/api/v1/drone/maintenance")
    def add_drone_maintenance(body: MaintenanceBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return drone_or_404().add_maintenance(body.kind, actor, body.note)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/drone/video.mjpg")
    async def drone_video(request: Request, _: str = Depends(require_auth)) -> StreamingResponse:
        drone = drone_or_404()
        worker = service.drone_video.get(drone.drone_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="no drone video configured")

        async def frames():
            last = None
            while not await request.is_disconnected():
                jpeg = worker.latest_jpeg
                if jpeg and jpeg is not last:
                    last = jpeg
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})

    # ---- drone companion computers ------------------------------------------------
    # Operator endpoints use the normal token. Companion endpoints authenticate the
    # device itself: a one-time pairing code, then HMAC-proven sessions.

    @app.get("/api/v1/drone/companions")
    def companions(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return [row | {"report": row.get("report", {})} for row in service.companions.list()]

    @app.post("/api/v1/drone/companions/code")
    def companion_code(body: PairingCodeBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        drone_id = body.drone_id or service.primary_drone
        if not drone_id or service.drone_manager(drone_id) is None:
            raise HTTPException(status_code=404, detail="no such drone")
        return service.companions.create_code(drone_id, actor)

    @app.delete("/api/v1/drone/companions/{companion_id}")
    def revoke_companion(companion_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return service.companions.revoke(companion_id, actor)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown companion") from None

    @app.post("/api/v1/drone/companion/pair")
    def companion_pair(body: CompanionPairBody) -> dict[str, Any]:
        try:
            return service.companions.pair(body.code, body.name, body.hardware)
        except PairingError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None

    @app.post("/api/v1/drone/companion/session")
    def companion_session(body: CompanionSessionBody) -> dict[str, Any]:
        try:
            return service.companions.open_session(body.companion_id, body.timestamp_ms, body.nonce, body.mac)
        except PairingError as error:
            raise HTTPException(status_code=401, detail=str(error)) from None

    @app.post("/api/v1/drone/companion/report")
    def companion_report(body: CompanionReportBody, request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        token = header[10:] if header.lower().startswith("companion ") else ""
        try:
            companion = service.companions.report(token, body.model_dump())
        except PairingError as error:
            raise HTTPException(status_code=401, detail=str(error)) from None
        return {"ok": True, "companion_id": companion.companion_id}

    # ---- setup: site configuration -------------------------------------------

    site = service.site

    def config_call(function, *args):
        try:
            return function(*args)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"unknown id {error}") from None
        except (SiteConfigError, ValueError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/config")
    def get_config(_: str = Depends(require_auth)) -> dict[str, Any]:
        return site.public_view()

    @app.post("/api/v1/config/cameras")
    def create_camera(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.save_camera, None, body, actor)

    @app.put("/api/v1/config/cameras/{camera_id}")
    def update_camera(camera_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.save_camera, camera_id, body, actor)

    @app.delete("/api/v1/config/cameras/{camera_id}")
    def delete_camera(camera_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.delete_camera, camera_id, actor)

    @app.put("/api/v1/config/camera-order")
    def reorder_cameras(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.reorder, list(body.get("order", [])), actor)
        return {"ok": True}

    @app.put("/api/v1/config/cameras/{camera_id}/zones")
    def save_zones(camera_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.set_zones, camera_id, list(body.get("zones", [])), list(body.get("privacy_masks", [])), actor)

    @app.put("/api/v1/config/cameras/{camera_id}/categories")
    def save_camera_categories(camera_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.set_categories, body.get("categories", {}), actor, camera_id)
        return {"ok": True}

    @app.put("/api/v1/config/categories")
    def save_categories(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.set_categories, body.get("categories", {}), actor)
        return {"ok": True}

    @app.put("/api/v1/config/map")
    def save_map(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.set_map, body, actor)
        return {"ok": True}

    @app.put("/api/v1/config/map/image")
    async def upload_map_image(request: Request, actor: str = Depends(require_auth)) -> dict[str, bool]:
        data = await request.body()
        config_call(site.set_map_image, data, actor)
        return {"ok": True}

    @app.get("/api/v1/config/map/image")
    def map_image(_: str = Depends(require_auth)) -> FileResponse:
        path = site.map_image_path()
        if path is None:
            raise HTTPException(status_code=404, detail="no map image")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.put("/api/v1/config/topology")
    def save_topology(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, bool]:
        config_call(site.set_topology, list(body.get("links", [])), actor)
        return {"ok": True}

    @app.get("/api/v1/cameras/{camera_id}/frame.jpg")
    def clean_frame(camera_id: str, _: str = Depends(require_auth)) -> Response:
        camera_or_404(camera_id)
        jpeg = runtime.latest_frame.get(camera_id)
        if not jpeg:
            raise HTTPException(status_code=503, detail="no frame yet")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- setup: camera onboarding ---------------------------------------------

    @app.get("/api/v1/onboarding/templates")
    def onboarding_templates(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return onboarding.templates()

    @app.get("/api/v1/onboarding/decode")
    def onboarding_decode(_: str = Depends(require_auth)) -> dict[str, Any]:
        return onboarding.decode_capabilities()

    @app.post("/api/v1/onboarding/local-devices")
    def onboarding_local_devices(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return onboarding.list_local_devices()

    @app.post("/api/v1/onboarding/test")
    def onboarding_test(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Test a camera before or after saving. Returns a draft id that the save
        call can reference; the draft (with any password) stays on the server."""
        if body.get("camera_id") and "source" not in body:
            camera = camera_or_404(body["camera_id"])
            result = onboarding.test_source(site.config_source(body["camera_id"]), runtime.secrets.get)
            return result | {"camera_id": camera["id"]}
        if body.get("draft_id"):  # e.g. a source staged by ONVIF inspection
            clean, password = config_call(site.draft, body["draft_id"])
            draft_id = body["draft_id"]
        else:
            source = dict(body.get("source") or {})
            password = source.pop("password", None)
            if body.get("camera_id"):  # editing: stream paths are write-only, so carry the stored ones forward
                existing = config_call(site.config_source, body["camera_id"])
                if existing.get("type") == source.get("type"):
                    source = {key: existing[key] for key in WRITE_ONLY_FIELDS if key in existing} | source
            draft_id, clean, password = config_call(site.make_draft, source, password)
        lookup_extra = {}
        test_source = dict(clean)
        if password:
            test_source["credential"] = "__draft__"
            lookup_extra["__draft__"] = password
        elif body.get("camera_id"):
            existing = site.config_source(body["camera_id"])
            if existing.get("credential") and existing.get("username") == clean.get("username"):
                test_source["credential"] = existing["credential"]
        result = onboarding.test_source(test_source, site.lookup_for(lookup_extra))
        runtime.store.audit(actor, "camera_connection_test", {"type": clean.get("type"), "host": clean.get("host", ""), "result": result.get("kind")})
        return result | {"draft_id": draft_id}

    @app.post("/api/v1/onboarding/onvif/discover")
    def onvif_discover(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return onvif.discover()

    @app.post("/api/v1/onboarding/onvif/inspect")
    def onvif_inspect(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Read profiles from an ONVIF camera and stage its streams as a draft source."""
        try:
            info = onvif.inspect(body["xaddr"], body.get("username", ""), body.get("password", ""))
        except onvif.OnvifError as error:
            return {"ok": False, "kind": str(error), "message": onboarding.ADVICE.get(str(error), f"ONVIF request failed ({error}).")}
        except KeyError:
            raise HTTPException(status_code=422, detail="xaddr is required") from None
        main = info["streams"].get("main") or {}
        source = {"type": "rtsp", "host": main.get("host"), "port": main.get("port", 554), "path": main.get("path"),
                  "username": body.get("username", ""), "template": "custom", "prefer_substream": True}
        if "sub" in info["streams"]:
            source["sub_path"] = info["streams"]["sub"]["path"]
        draft_id, _, _ = config_call(site.make_draft, source, body.get("password") or None)
        return {
            "ok": True, "draft_id": draft_id, "device": info["device"],
            "streams": {which: stream["profile"] for which, stream in info["streams"].items()},
        }

    # ---- setup: network diagnostics -------------------------------------------
    # Read-only: Sentinel inspects and probes, and explains what to change. It never
    # changes router, firewall, computer or device settings.

    @app.get("/api/v1/onboarding/network")
    def network_overview(refresh: bool = False, _: str = Depends(require_auth)) -> dict[str, Any]:
        snapshot = service.network.snapshot(refresh=refresh)
        return {
            "network": snapshot.public_view(),
            "findings": [finding.as_dict() for finding in netdiag.analyze_network(snapshot)],
            "brands": netdiag.guidance.public_brands(),
        }

    @app.post("/api/v1/onboarding/network/diagnose")
    def network_diagnose(body: DiagnoseBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            report = netdiag.troubleshoot(
                service.network, body.host, brand=body.brand, device_mask=body.device_mask, device_gateway=body.device_gateway,
                rtsp_port=body.rtsp_port, http_port=body.http_port, discover=body.discover,
            )
        except netdiag.TargetRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        runtime.store.audit(actor, "network_diagnosis", {
            "host": report["host"], "reachable": report["reachable"],
            "findings": [f["code"] for f in report["findings"] if f["status"] in ("confirmed", "suspected")],
        })
        return report

    @app.get("/api/v1/onboarding/guidance/new-cameras")
    def new_camera_guidance(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        """Power, network and pairing problems when installing new PoE or Wi-Fi cameras."""
        return netdiag.guidance.new_camera_guide()

    @app.post("/api/v1/onboarding/network/discover")
    def network_discover(body: DiscoverBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(status_code=422, detail="Confirm that you own or manage this network before searching it.")
        result = netdiag.discover_devices(service.network, sadp=body.sadp, onvif=body.onvif, port_check=body.port_check)
        runtime.store.audit(actor, "network_discovery", {"methods": result["methods"], "networks": result["networks_searched"], "found": len(result["devices"])})
        return result

    @app.post("/api/v1/onboarding/channels")
    def recorder_channels(body: ChannelsBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        """Find a recorder's channels and stage one draft per channel that answered,
        so the dashboard can add several cameras without re-sending the password."""
        if body.draft_id:
            clean, password = config_call(site.draft, body.draft_id)
        elif body.source:
            source = dict(body.source)
            password = source.pop("password", None)
            _, clean, password = config_call(site.make_draft, source, password)
        else:
            raise HTTPException(status_code=422, detail="source or draft_id is required")
        try:
            netdiag.check_target(str(clean.get("host", "")))
        except netdiag.TargetRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        scan_source = dict(clean, credential="__draft__") if password else clean
        result = onboarding.scan_channels(scan_source, site.lookup_for({"__draft__": password} if password else {}),
                                          probe=service.network.rtsp_probe, max_channels=body.max_channels)
        for channel in result["channels"]:
            if channel["main"]["kind"] == "ok":
                channel["draft_id"] = config_call(site.make_draft, dict(clean, channel=channel["channel"]), password or None)[0]
        runtime.store.audit(actor, "channel_scan", {"host": clean.get("host", ""), "result": result["kind"],
                                                    "found": sum(1 for c in result["channels"] if c.get("draft_id"))})
        return result

    # ---- devices and installation ----------------------------------------------------
    # Registration records what is installed. A device counts as connected,
    # delivering video, or AI-verified only from data Sentinel has received.

    def installation_call(function, *args):
        try:
            return function(*args)
        except InstallationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/devices")
    def list_devices(_: str = Depends(require_auth)) -> dict[str, Any]:
        return {"devices": service.devices_view()}

    @app.get("/api/v1/devices/catalog")
    def device_catalog(_: str = Depends(require_auth)) -> dict[str, Any]:
        return devices.catalog()

    @app.post("/api/v1/devices")
    def register_device(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.save_device, None, body, actor)

    @app.put("/api/v1/devices/{device_id}")
    def update_device(device_id: str, body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.save_device, device_id, body, actor)

    @app.delete("/api/v1/devices/{device_id}")
    def remove_device(device_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(service.remove_device, device_id, actor)

    @app.post("/api/v1/devices/{device_id}/test")
    def test_device(device_id: str, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(service.test_sensor, device_id, actor)

    @app.put("/api/v1/config/drone")
    def save_drone(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        return config_call(site.set_drone, body, actor)

    @app.put("/api/v1/config/notifications")
    def save_notifications(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        config_call(site.set_notifications, body, actor)
        return service.notification_view()

    @app.get("/api/v1/installation")
    def installation(_: str = Depends(require_auth)) -> dict[str, Any]:
        return service.installation_view()

    @app.put("/api/v1/installation")
    def update_installation(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        installation_call(service.installation.update, body)
        if body.get("path"):
            runtime.store.audit(actor, "installation_path_chosen", {"path": body["path"]})
        return service.installation_view()

    @app.post("/api/v1/installation/property")
    def save_property(body: PropertyBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        config_call(site.set_site_name, body.name, actor)
        map_change: dict[str, Any] = {"width_m": body.width_m, "height_m": body.height_m}
        if body.latitude is not None and body.longitude is not None:
            map_change["origin_geo"] = [body.latitude, body.longitude]  # stored locally only, for the drone geofence
        config_call(site.set_map, map_change, actor)
        installation_call(service.installation.update, {"property": {"property_type": body.property_type, "notes": body.notes},
                                                        "steps": {"property": "done"}})
        return service.installation_view()

    @app.post("/api/v1/installation/recommend")
    def recommend(body: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
        result = planner.recommend(body)
        service.installation.set_recommendation(body, result)
        return result

    @app.get("/api/v1/installation/plan")
    def get_plan(_: str = Depends(require_auth)) -> dict[str, Any]:
        return service.installation.state["plan"]

    @app.put("/api/v1/installation/plan")
    def save_plan(body: dict[str, Any], actor: str = Depends(require_auth)) -> dict[str, Any]:
        plan = installation_call(service.installation.set_plan, body)
        runtime.store.audit(actor, "installation_plan_saved", {"items": len(plan["items"])})
        return plan | {"coverage": service.coverage_analysis(plan)}

    @app.post("/api/v1/installation/plan/coverage")
    def plan_coverage(body: dict[str, Any], _: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            plan = planner.clean_plan(body) if body.get("items") is not None else None
        except planner.PlanError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        result = service.coverage_analysis(plan)
        return result | {"estimate": planner.estimate(plan or service.installation.state["plan"], service.site.config.get("cameras", []))}

    def build_report() -> dict[str, Any]:
        state = service.installation.state
        config = service.site.config
        site_map = config.get("site", {}).get("map", {})
        return planner.build_report(
            site_name=config["site"].get("name", config["site"]["id"]), path=state["path"], property_info=state["property"], plan=state["plan"],
            width_m=float(site_map.get("width_m", 40)), height_m=float(site_map.get("height_m", 30)), installed=config.get("cameras", []),
            devices=[d for d in service.devices_view() if d["category"] != "hub"], recommendation=state.get("recommendation"),
        )

    @app.get("/api/v1/installation/report")
    def installation_report(_: str = Depends(require_auth)) -> dict[str, Any]:
        return build_report()

    @app.get("/api/v1/installation/report.html", response_class=HTMLResponse)
    def installation_report_html(download: bool = False, _: str = Depends(require_auth)) -> HTMLResponse:
        headers = {"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:"}
        if download:
            headers["Content-Disposition"] = 'attachment; filename="sentinel-installation-plan.html"'
        return HTMLResponse(planner.render_html(build_report()), headers=headers)

    @app.post("/api/v1/installation/test-notification")
    def test_notification(actor: str = Depends(require_auth)) -> dict[str, Any]:
        return service.send_test_notification(actor)

    @app.post("/api/v1/installation/complete")
    def complete_installation(body: CompleteBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        view = service.installation_view()
        readiness = view["readiness"]
        if not readiness["can_complete"]:
            raise HTTPException(status_code=409, detail="Installation cannot be completed yet: " + "; ".join(readiness["blocking"]))
        if readiness["open_issues"] and not body.acknowledge_issues:
            raise HTTPException(status_code=409, detail=f"{len(readiness['open_issues'])} open issue(s) must be acknowledged first")
        service.installation.complete(actor, readiness["open_issues"])
        runtime.store.audit(actor, "installation_completed", {"open_issues": len(readiness["open_issues"]), "statement": readiness["statement"]["state"]})
        return service.installation_view()

    @app.post("/api/v1/installation/reopen")
    def reopen_installation(actor: str = Depends(require_auth)) -> dict[str, Any]:
        service.installation.reopen()
        runtime.store.audit(actor, "installation_reopened", {})
        return service.installation_view()

    @app.post("/api/v1/installation/reset")
    def reset_installation(body: ResetBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        service.installation.reset(keep_plan=body.keep_plan)
        runtime.store.audit(actor, "installation_reset", {"keep_plan": body.keep_plan})
        return service.installation_view()

    from .assistant.routes import add_routes  # Sentinel AI Guide: /api/v1/assistant

    add_routes(app, service, require_auth)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app
