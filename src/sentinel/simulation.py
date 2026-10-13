"""Scripted multi-camera scenarios so the full brain (tracking, zones, events,
incidents, alerts, dashboard) runs and is testable without any cameras."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Detection
from .vision import Frame

SIM_WIDTH, SIM_HEIGHT = 640, 360
DEFAULT_SIZES = {"person": (0.06, 0.24), "car": (0.28, 0.2), "truck": (0.32, 0.26), "dog": (0.07, 0.07)}


@dataclass
class Scenario:
    name: str
    duration_s: float
    actors: list[dict[str, Any]]
    seed: int = 7
    loop: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, default_name=Path(path).stem)

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_name: str = "scenario") -> "Scenario":
        return cls(
            name=data.get("name", default_name),
            duration_s=float(data["duration_s"]),
            actors=list(data["actors"]),
            seed=int(data.get("seed", 7)),
            loop=bool(data.get("loop", True)),
        )

    def cameras(self) -> set[str]:
        return {wp["camera"] for actor in self.actors for wp in actor["path"] if wp.get("camera")}

    def detections_at(self, camera_id: str, t_s: float) -> list[Detection]:
        detections = []
        for actor in self.actors:
            position = _position(actor["path"], camera_id, t_s)
            if position is None:
                continue
            rng = random.Random(f"{self.seed}:{actor['id']}:{camera_id}:{round(t_s * 20)}")
            if rng.random() < actor.get("miss_rate", 0.0):
                continue
            x, y, scale = position
            noise = actor.get("noise", 0.0)
            x += rng.uniform(-noise, noise)
            y += rng.uniform(-noise, noise)
            width, height = actor.get("size", DEFAULT_SIZES.get(actor["label"], (0.08, 0.2)))
            width, height = width * scale, height * scale
            bbox = (x - width / 2, y - height, width, height)
            detections.append(Detection(actor["label"], actor.get("confidence", 0.85), _clip(bbox)))
        return detections


def _position(path: list[dict[str, Any]], camera_id: str, t_s: float) -> tuple[float, float, float] | None:
    for start, end in zip(path, path[1:]):
        if not (start["t"] <= t_s < end["t"]):
            continue
        if start.get("camera") != camera_id or end.get("camera") != camera_id:
            return None
        span = end["t"] - start["t"]
        fraction = (t_s - start["t"]) / span if span > 0 else 0.0
        lerp = lambda key, default: start.get(key, default) + (end.get(key, default) - start.get(key, default)) * fraction
        return (lerp("x", 0.5), lerp("y", 0.9), lerp("scale", 1.0))
    return None


def _clip(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, w, h = bbox
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + w), min(1.0, y + h)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


class SimulatedSource:
    """Renders a scenario for one camera in real time; ground-truth boxes ride
    along in Frame.truth for the scripted detector."""

    def __init__(self, camera: dict[str, Any], scenario: Scenario) -> None:
        self.camera = camera
        self.scenario = scenario
        self.started = time.monotonic()
        self.connected = True
        self.last_error = ""

    def read(self) -> Frame | None:
        import cv2
        import numpy as np

        elapsed = time.monotonic() - self.started
        if self.scenario.loop:
            elapsed %= self.scenario.duration_s
        elif elapsed > self.scenario.duration_s:
            return None
        truth = self.scenario.detections_at(self.camera["id"], elapsed)
        image = np.full((SIM_HEIGHT, SIM_WIDTH, 3), (48, 52, 46), dtype=np.uint8)
        cv2.rectangle(image, (0, int(SIM_HEIGHT * 0.45)), (SIM_WIDTH, SIM_HEIGHT), (70, 82, 64), -1)
        cv2.putText(image, f"SIM {self.camera.get('name', self.camera['id'])}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        cv2.putText(image, f"t={elapsed:5.1f}s", (SIM_WIDTH - 100, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        for det in truth:
            x, y, w, h = det.bbox
            color = (90, 90, 230) if det.label == "person" else (230, 170, 60)
            cv2.rectangle(image, (int(x * SIM_WIDTH), int(y * SIM_HEIGHT)), (int((x + w) * SIM_WIDTH), int((y + h) * SIM_HEIGHT)), color, -1)
        return Frame(image=image, timestamp_ms=int(time.time() * 1000), truth=truth)

    def close(self) -> None:
        self.connected = False


def load_scenario_for(config: dict[str, Any], scenario_path: str) -> Scenario:
    path = Path(scenario_path)
    if not path.is_absolute() and not path.exists():
        path = Path(config.get("_config_dir", ".")) / scenario_path
    return Scenario.load(path)
