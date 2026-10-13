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

from .config import public_camera_view
from .drone import FlightCommandRejected
from .service import SentinelService

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
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

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

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app
