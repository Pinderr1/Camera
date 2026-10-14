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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import backup, netdiag, onboarding, onvif
from .config import public_camera_view
from .drone import FlightCommandRejected
from .service import SentinelService
from .site_config import WRITE_ONLY_FIELDS, SiteConfigError

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "sentinel_session"


class SessionBody(BaseModel):
    token: str


class SettingsBody(BaseModel):
    arm_mode: Literal["disarmed", "home", "away"] | None = None
    sensitivity: Literal["low", "normal", "high"] | None = None
    notifications_enabled: bool | None = None


class ResolveBody(BaseModel):
    outcome: Literal["expected_activity", "false_alarm", "real_concern", "unclear"]


class InspectionBody(BaseModel):
    zone_id: str | None = None
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


class DroneCommandBody(BaseModel):
    action: Literal["takeoff", "goto", "hold", "return_home", "land"]
    params: dict[str, float] = Field(default_factory=dict)


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
            else:
                request = drone.request_inspection(requested_by=actor, lat=body.lat, lon=body.lon, label="custom point", incident_id=body.incident_id)
        except (FlightCommandRejected, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return request.as_dict()

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

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app
