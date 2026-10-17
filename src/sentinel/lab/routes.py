"""Test Lab API: /api/v1/lab (docs/api.md)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


class RunBody(BaseModel):
    clip_id: str
    profile: str = "current"
    fps: float = Field(5.0, ge=1, le=10)
    arm_mode: Literal["home", "away", "night", "vacation", "disarmed"] = "away"
    species: bool = True
    max_seconds: float = Field(120.0, ge=2, le=600)


class CorrectionBody(BaseModel):
    kind: str
    frame: int | None = None
    bbox: list[float] | None = None
    label: str | None = None
    detection: int | None = None
    event_id: str | None = None
    track_id: str | None = None
    note: str | None = Field(None, max_length=500)


def add_routes(app: FastAPI, service: Any, require_auth: Any) -> None:
    def lab():
        if getattr(service, "lab", None) is None:
            raise HTTPException(status_code=503, detail="Test Lab is not available (OpenCV missing?)")
        return service.lab

    def run_of(run_id: str):
        try:
            return lab().get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown run") from None

    @app.get("/api/v1/lab/catalog")
    def catalog(_: str = Depends(require_auth)) -> dict[str, Any]:
        return lab().catalog()

    @app.get("/api/v1/lab/runs")
    def runs(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return [{key: view.get(key) for key in ("id", "created", "state", "clip", "profile", "options", "progress", "summary", "error", "corrections")}
                for view in lab().list_runs()]

    @app.post("/api/v1/lab/runs")
    def start(body: RunBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            view = lab().start(body.clip_id, profile=body.profile, fps=body.fps, arm_mode=body.arm_mode,
                               species=body.species, max_seconds=body.max_seconds, actor=actor)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"unknown clip or profile: {error}") from None
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        service.runtime.store.audit(actor, "lab_run_started", {"run": view["id"], "clip": body.clip_id, "profile": body.profile})
        return view

    @app.get("/api/v1/lab/runs/{run_id}")
    def run(run_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        return run_of(run_id).view()

    @app.get("/api/v1/lab/runs/{run_id}/frames")
    def frames(run_id: str, start: int = 0, limit: int = 2000, _: str = Depends(require_auth)) -> dict[str, Any]:
        target = run_of(run_id)
        records = target.frame_records(max(0, start), max(1, min(limit, 5000)))
        return {"start": start, "frames": records, "state": target.meta["state"], "total": target.meta["progress"]["frames"]}

    @app.get("/api/v1/lab/runs/{run_id}/frame/{n}.jpg")
    def frame_image(run_id: str, n: int, _: str = Depends(require_auth)) -> FileResponse:
        path = run_of(run_id).dir / "frames" / f"{n:05d}.jpg"
        if n < 0 or not path.exists():
            raise HTTPException(status_code=404, detail="frame not available")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/v1/lab/runs/{run_id}/cancel")
    def cancel(run_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        run_of(run_id)
        return lab().cancel(run_id)

    @app.delete("/api/v1/lab/runs/{run_id}")
    def delete(run_id: str, actor: str = Depends(require_auth)) -> dict[str, bool]:
        run_of(run_id)
        try:
            lab().delete(run_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        service.runtime.store.audit(actor, "lab_run_deleted", {"run": run_id})
        return {"ok": True}

    @app.get("/api/v1/lab/runs/{run_id}/corrections")
    def corrections(run_id: str, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return run_of(run_id).corrections()

    @app.post("/api/v1/lab/runs/{run_id}/corrections")
    def add_correction(run_id: str, body: CorrectionBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        run_of(run_id)
        try:
            return lab().add_correction(run_id, body.model_dump(exclude_none=True), actor)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None

    @app.delete("/api/v1/lab/runs/{run_id}/corrections/{correction_id}")
    def remove_correction(run_id: str, correction_id: str, _: str = Depends(require_auth)) -> dict[str, bool]:
        run_of(run_id)
        lab().remove_correction(run_id, correction_id)
        return {"ok": True}

    @app.get("/api/v1/lab/training")
    def training(_: str = Depends(require_auth)) -> dict[str, Any]:
        from ..training import status

        return status(service.storage_dir)
