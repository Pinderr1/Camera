"""Draw benchmark overlays (zones, annotations, detections, tracks, events)
onto source frames. Shared by the web viewer and the MP4 exporter."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..vision import apply_privacy_masks
from . import degrade as degrade_module
from .labels import GroundTruth, load_labels

LAYERS = ("zones", "gt", "dets", "tracks", "events")


class ClipSource:
    """Random access to a clip's frames plus everything logged for it."""

    def __init__(self, session_dir: Path, clip: dict[str, Any], camera: dict[str, Any]) -> None:
        import cv2

        self.clip = clip
        self.camera = camera
        self.capture = cv2.VideoCapture(clip["video"])
        self.width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.position = -1
        self.lock = threading.Lock()
        clip_dir = session_dir / "clips" / clip["clip_id"]
        self.frames = [json.loads(line) for line in (clip_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.by_index = {row["i"]: row for row in self.frames}
        events_path = clip_dir / "events.json"
        self.events = json.loads(events_path.read_text(encoding="utf-8")) if events_path.exists() else {"predicted": [], "expected": []}
        self.gt: GroundTruth | None = None
        if clip.get("labels") and Path(clip["labels"]["path"]).exists():
            self.gt = load_labels(clip["labels"], self.width, self.height, clip["source_fps"])
        self.start_ms = self.frames[0]["ts"] - int(self.frames[0]["t"] * 1000) if self.frames else 0

    def timeline(self) -> dict[str, Any]:
        matches = self.events.get("matches") or {}
        matched_pred = {p for _, p, _ in matches.get("matched", [])}
        matched_exp = {e for e, _, _ in matches.get("matched", [])}
        return {
            "clip": self.clip,
            "frames": [{"i": row["i"], "t": row["t"], "d": len(row["dets"]), "k": len(row["tracks"]), "run": row["detected"]} for row in self.frames],
            "predicted": [
                {"t": (event["timestamp_ms"] - self.start_ms) / 1000, "type": event["event_type"], "severity": event["severity"],
                 "summary": event["summary"], "zone": event.get("zone_id", ""), "matched": index in matched_pred}
                for index, event in enumerate(self.events.get("predicted", []))
            ],
            "expected": [
                {"t": event["time_s"], "type": event["event_type"], "zone": event.get("zone_id", ""), "source": event.get("source", ""),
                 "note": event.get("note", ""), "matched": index in matched_exp}
                for index, event in enumerate(self.events.get("expected", []))
            ],
            "notifications": [{"t": (n["timestamp_ms"] - self.start_ms) / 1000, "title": n["title"], "severity": n["severity"]} for n in self.events.get("notifications", [])],
            "has_gt": self.gt is not None,
        }

    def read(self, index: int):
        import cv2

        with self.lock:
            if index < self.position or index - self.position > 60:
                self.capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                self.position = index - 1
            while self.position < index - 1:
                self.capture.grab()
                self.position += 1
            ok, image = self.capture.read()
            self.position += 1
        return image if ok else None

    def render(self, index: int, layers: set[str]):
        import cv2

        image = self.read(index)
        if image is None:
            return None
        if self.clip.get("degradation"):
            image = degrade_module.apply(image, self.clip["degradation"], index)
        image = apply_privacy_masks(image, self.camera.get("privacy_masks", []))
        scale = 2 if self.width < 500 else 1  # upscale tiny clips so labels stay legible
        if scale > 1:
            image = cv2.resize(image, (self.width * scale, self.height * scale), interpolation=cv2.INTER_NEAREST)
        height, width = image.shape[:2]

        def box(bbox, color, thickness=1):
            x, y, w, h = bbox
            p0 = (int(x * width), int(y * height))
            p1 = (int((x + w) * width), int((y + h) * height))
            cv2.rectangle(image, p0, p1, color, thickness)
            return p0, p1

        if "zones" in layers:
            for zone in self.camera.get("zones", []):
                points = [(int(x * width), int(y * height)) for x, y in zone["polygon"]]
                color = (60, 60, 230) if zone["type"] == "restricted" else (0, 200, 255)
                for k in range(len(points)):
                    cv2.line(image, points[k], points[(k + 1) % len(points)], color, 1)
                cv2.putText(image, zone.get("name", zone["id"]), (points[0][0] + 3, points[0][1] + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        if "gt" in layers and self.gt is not None:
            for gt in self.gt.at_frame(int(round(index / self.clip["source_fps"] * self.gt.fps))):
                color = (120, 120, 120) if gt.ignore else (80, 220, 80)
                p0, _ = box(gt.bbox, color)
                cv2.putText(image, gt.track_id, (p0[0], p0[1] - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
        row = self.by_index.get(index) or self._nearest(index)
        if row and "dets" in layers:
            for label, confidence, *bbox in row["dets"]:
                p0, p1 = box(bbox, (0, 230, 230))
                cv2.putText(image, f"{label} {confidence:.2f}", (p0[0], p1[1] + 11), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 230, 230), 1)
        if row and "tracks" in layers:
            for track_id, site_id, label, *bbox in row["tracks"]:
                color = _id_color(site_id or track_id)
                p0, _ = box(bbox, color, 2)
                cv2.putText(image, f"{track_id.split(':')[-1]} {site_id}", (p0[0], max(10, p0[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        if "events" in layers and row:
            t = row["t"]
            recent = [e for e in self.timeline_cache() if 0 <= t - e["t"] <= 2.0]
            for k, event in enumerate(recent[:4]):
                text = event["text"]
                cv2.putText(image, text, (8, height - 10 - 18 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
                cv2.putText(image, text, (8, height - 10 - 18 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.5, event["color"], 1)
        if row:
            cv2.putText(image, f"frame {index}  t={row['t']:.1f}s  {'detector ran' if row['detected'] else 'gated'}", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        return image

    def timeline_cache(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_banner"):
            data = self.timeline()
            self._banner = [{"t": e["t"], "text": f"EVENT {e['type']}: {e['summary']}", "color": (80, 80, 255)} for e in data["predicted"] if e["type"] != "activity"]
            self._banner += [{"t": n["t"], "text": f"NOTIFY {n['title']}", "color": (0, 160, 255)} for n in data["notifications"]]
        return self._banner

    def _nearest(self, index: int):
        """Frames between detector samples show the most recent processed state."""
        earlier = [row for row in self.frames if row["i"] <= index]
        return earlier[-1] if earlier else None

    def close(self) -> None:
        self.capture.release()


def _id_color(key: str) -> tuple[int, int, int]:
    value = sum(ord(c) * (k + 7) for k, c in enumerate(key))
    palette = [(255, 99, 71), (60, 179, 113), (65, 105, 225), (238, 130, 238), (255, 165, 0), (0, 206, 209), (220, 20, 60), (154, 205, 50)]
    return palette[value % len(palette)]


def load_session(session_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = json.loads((session_dir / "result.json").read_text(encoding="utf-8"))
    config = json.loads((session_dir / "site_config.json").read_text(encoding="utf-8"))
    return result, config


def export_video(session_dir: Path, clip_id: str, target: Path, layers: set[str] = set(LAYERS)) -> Path:
    import cv2

    result, config = load_session(session_dir)
    clip = next(item for item in result["clips"] if item["clip_id"] == clip_id)
    camera = next(item for item in config["cameras"] if item["id"] == clip["camera_id"])
    source = ClipSource(session_dir, clip, camera)
    writer = None
    try:
        for row in source.frames:
            image = source.render(row["i"], layers)
            if image is None:
                break
            if writer is None:
                height, width = image.shape[:2]
                writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*"mp4v"), float(result.get("bench_fps") or clip["source_fps"]), (width, height))
            writer.write(image)
    finally:
        if writer is not None:
            writer.release()
        source.close()
    return target
