"""Live benchmark: real CameraWorker threads on RTSP/HTTP/USB streams (or
real-time paced files), all cameras concurrently, for a fixed duration.

Measures what offline replay cannot: capture-to-decision latency, achieved
frame rate under contention, frames dropped by the latest-frame reader, and
CPU/memory with every camera running at once. Processed frames are recorded
so the run can be stepped through in the viewer. Live streams have no frame
aligned annotations, so no accuracy is computed here."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import cameras as enabled_cameras
from ..models import Notification
from ..notify import CallbackNotifier
from ..pipeline import CameraWorker
from ..runtime import SentinelRuntime
from .metrics import percentiles
from .runner import HEALTH_EVENTS, ResourceSampler, _count_types, _host, safe_config


class _CameraLog:
    def __init__(self, camera: dict[str, Any], directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.camera = camera
        self.directory = directory
        self.frames = (directory / "frames.jsonl").open("w", encoding="utf-8")
        self.recording_path = directory / "recording.mp4"
        self.writer = None
        self.count = 0
        self.first_ms: int | None = None
        self.last_ms = 0
        self.latency: list[float] = []
        self.timings: dict[str, list[float]] = {"detect": [], "track": [], "engine": []}
        self.frames_read = 0
        self.size = (0, 0)
        self.lock = threading.Lock()

    def observe(self, record: dict[str, Any], image: Any, runtime: SentinelRuntime) -> None:
        import cv2

        with self.lock:
            if self.writer is None:
                height, width = image.shape[:2]
                self.size = (width, height)
                self.writer = cv2.VideoWriter(str(self.recording_path), cv2.VideoWriter_fourcc(*"mp4v"), float(self.camera.get("fps", 5)), (width, height))
            self.writer.write(image)
            self.first_ms = self.first_ms or record["capture_ms"]
            self.last_ms = record["capture_ms"]
            self.latency.append(record["done_ms"] - record["capture_ms"])
            if record["detected"]:
                for key in self.timings:
                    self.timings[key].append(record["timings"][key])
            self.frames_read = record["frames_read"] or self.frames_read
            site_ids = runtime.engine.correlator.track_to_site
            self.frames.write(
                json.dumps(
                    {
                        "i": self.count,
                        "t": round((record["capture_ms"] - self.first_ms) / 1000, 3),
                        "ts": record["capture_ms"],
                        "detected": record["detected"],
                        "latency_ms": record["done_ms"] - record["capture_ms"],
                        "dets": [[d.label, round(d.confidence, 3), *[round(v, 4) for v in d.bbox]] for d in record["detections"]],
                        "tracks": [[t.track_id, site_ids.get(t.track_id, ""), t.label, *[round(v, 4) for v in t.bbox]] for t in record["tracks"]],
                        "ms": {key: round(value, 2) for key, value in record["timings"].items()},
                    }
                )
                + "\n"
            )
            self.count += 1

    def close(self) -> None:
        with self.lock:
            if self.writer is not None:
                self.writer.release()
            self.frames.close()


def run_live(config: dict[str, Any], out_dir: str | Path, duration_s: float, *, session_id: str = "live") -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for channel in config.get("alerts", {}).get("channels", []):
        channel["enabled"] = False
    config.setdefault("drone", {})["enabled"] = False
    config.setdefault("recording", {})["enabled"] = False

    notifications: list[Notification] = []
    events: list[dict[str, Any]] = []
    runtime = SentinelRuntime(config, storage_dir=out / "store", notifiers=[CallbackNotifier("bench", notifications.append)])
    runtime.bus.subscribe(lambda topic, payload: events.append(payload) if topic == "event" else None)
    logs = {camera["id"]: _CameraLog(camera, out / "clips" / camera["id"]) for camera in enabled_cameras(config)}
    workers = [
        CameraWorker(camera, runtime, on_frame=lambda record, image, log=logs[camera["id"]]: log.observe(record, image, runtime))
        for camera in enabled_cameras(config)
    ]
    sampler = ResourceSampler()
    sampler.start()
    started = time.perf_counter()
    for worker in workers:
        worker.start()
    try:
        while time.perf_counter() - started < duration_s:
            time.sleep(1.0)
            runtime.housekeeping()
    finally:
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=10)
        wall_s = time.perf_counter() - started
        resources = sampler.stop()
        for log in logs.values():
            log.close()
        runtime.dispatcher.stop()

    clips = []
    for camera_id, log in logs.items():
        camera_events = [event for event in events if event["camera_id"] == camera_id and event["event_type"] not in HEALTH_EVENTS]
        span_s = (log.last_ms - log.first_ms) / 1000 if log.first_ms else 0.0
        status = runtime.camera_status.get(camera_id, {})
        clips.append(
            {
                "clip_id": camera_id,
                "camera_id": camera_id,
                "video": str(log.recording_path),
                "source_type": log.camera["source"]["type"],
                "source_fps": float(log.camera.get("fps", 5)),
                "resolution": list(log.size),
                "conditions": list(log.camera.get("conditions", ["live"])),
                "label_source": None,
                "labeled_boxes": False,
                "labeled_events": False,
                "expect_no_notifications": bool(log.camera.get("expect_no_notifications", False)),
                "frames_processed": log.count,
                "frames_read_from_source": log.frames_read or None,
                "frames_dropped_by_reader": (log.frames_read - log.count) if log.frames_read else None,
                "detector_runs": len(log.timings["detect"]),
                "video_seconds": round(span_s, 2),
                "processing_wall_s": round(wall_s, 2),
                "throughput_fps": round(log.count / span_s, 2) if span_s else None,
                "capture_to_decision_ms": percentiles(log.latency),
                "timings_ms": {**{key: percentiles(values) for key, values in log.timings.items()}, "total": percentiles(log.latency), "decode": percentiles([])},
                "detection": None,
                "tracking": None,
                "events": None,
                "event_counts": _count_types(camera_events),
                "notifications": sum(1 for n in notifications if camera_id in n.camera_ids),
                "notification_match": None,
                "false_alerts_per_hour": round(sum(1 for n in notifications if camera_id in n.camera_ids) * 3600 / span_s, 1)
                if span_s and log.camera.get("expect_no_notifications")
                else None,
                "camera_status": {key: status.get(key) for key in ("connected", "error", "offline_reported")},
            }
        )
        (out / "clips" / camera_id / "events.json").write_text(
            json.dumps({"predicted": camera_events, "expected": [], "matches": None, "notifications": [n.as_dict() for n in notifications if camera_id in n.camera_ids]}, indent=2, default=str),
            encoding="utf-8",
        )
    runtime.close()
    result = {
        "session_id": session_id,
        "mode": "live",
        "clips": clips,
        "wall_s": round(wall_s, 2),
        "resources": resources,
        "host": _host(),
        "degradation": None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out / "site_config.json").write_text(json.dumps(safe_config(config), indent=2, default=str), encoding="utf-8")
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result
