"""Local playback/debug viewer for benchmark runs (binds to localhost only)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse

from .render import LAYERS, ClipSource, load_session

PAGE = Path(__file__).parent / "web" / "viewer.html"


def create_viewer(runs_root: str | Path) -> FastAPI:
    root = Path(runs_root).resolve()
    sources: dict[tuple[str, str], ClipSource] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for source_ in sources.values():
            source_.close()  # release video handles
        sources.clear()

    app = FastAPI(title="Sentinel benchmark viewer", docs_url=None, redoc_url=None, lifespan=lifespan)

    def session_dir(session: str) -> Path:
        path = (root / session).resolve()
        if path != root and root not in path.parents or not (path / "result.json").exists():
            raise HTTPException(status_code=404, detail="unknown session")
        return path

    def source(session: str, clip_id: str) -> ClipSource:
        key = (session, clip_id)
        if key not in sources:
            directory = session_dir(session)
            result, config = load_session(directory)
            clip = next((item for item in result["clips"] if item["clip_id"] == clip_id), None)
            if clip is None:
                raise HTTPException(status_code=404, detail="unknown clip")
            camera = next(item for item in config["cameras"] if item["id"] == clip["camera_id"])
            sources[key] = ClipSource(directory, clip, camera)
        return sources[key]

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(PAGE)

    @app.get("/api/sessions")
    def sessions() -> list[dict[str, Any]]:
        found = []
        for result_path in sorted(root.rglob("result.json")):
            result = json.loads(result_path.read_text(encoding="utf-8"))
            found.append(
                {
                    "session": result_path.parent.relative_to(root).as_posix(),
                    "mode": result.get("mode"),
                    "degradation": result.get("degradation"),
                    "clips": [clip["clip_id"] for clip in result["clips"]],
                }
            )
        return found

    @app.get("/api/report")
    def report(session: str) -> dict[str, Any]:
        result, _ = load_session(session_dir(session))
        return result

    @app.get("/api/timeline")
    def timeline(session: str, clip: str) -> dict[str, Any]:
        return source(session, clip).timeline()

    @app.get("/api/frame")
    def frame(session: str, clip: str, i: int, layers: str = ",".join(LAYERS)) -> Response:
        import cv2

        image = source(session, clip).render(i, set(layers.split(",")) & set(LAYERS))
        if image is None:
            raise HTTPException(status_code=404, detail="frame not available")
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(jpeg.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    return app
