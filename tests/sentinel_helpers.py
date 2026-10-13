"""Shared fixtures for Sentinel tests: a tiny two-camera site and a driver that
pushes scripted detections through the real tracker and engine."""

from __future__ import annotations

import copy
from datetime import datetime

from sentinel.engine import TemporalEventEngine
from sentinel.models import Detection
from sentinel.tracker import IoUTracker, TrackerConfig

FULL = [[0, 0], [1, 0], [1, 1], [0, 1]]
LEFT = [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]
RIGHT = [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]

BASE_CONFIG = {
    "site": {"id": "test_site", "map": {"width_m": 20, "height_m": 20, "origin_geo": [45.0, -75.0]}},
    "cameras": [
        {
            "id": "cam_a",
            "source": {"type": "simulated", "scenario": "none.json"},
            "zones": [
                {"id": "yard", "name": "Yard", "type": "yard", "polygon": LEFT},
                {"id": "shed", "name": "Shed", "type": "restricted", "polygon": RIGHT},
            ],
        },
        {
            "id": "cam_b",
            "source": {"type": "simulated", "scenario": "none.json"},
            "zones": [{"id": "porch", "name": "Porch", "type": "entrance", "polygon": FULL}],
        },
    ],
    "topology": {"links": [{"from": "cam_a", "to": "cam_b", "min_transit_s": 2, "max_transit_s": 20}]},
    "rules": [],
}

NOON_MS = int(datetime(2026, 10, 12, 12, 0, 0).timestamp() * 1000)


def make_config(rules=None, **overrides):
    config = copy.deepcopy(BASE_CONFIG)
    config["rules"] = rules or []
    config.update(overrides)
    return config


def person(x, y, label="person", size=(0.06, 0.2), confidence=0.9):
    """Detection whose ground anchor (bottom-centre) is at x, y."""
    w, h = size
    return Detection(label, confidence, (x - w / 2, y - h, w, h))


class Driver:
    """Steps virtual time at a fixed rate, feeding each camera's detections."""

    def __init__(self, engine: TemporalEventEngine, start_ms: int = NOON_MS, fps: float = 5.0):
        self.engine = engine
        self.now = start_ms
        self.step_ms = int(1000 / fps)
        self.trackers = {
            camera["id"]: IoUTracker(camera["id"], TrackerConfig(min_hits=2))
            for camera in engine.config["cameras"]
        }
        self.events = []

    def run(self, seconds: float, detections_by_camera: dict | None = None):
        detections_by_camera = detections_by_camera or {}
        for _ in range(int(seconds * 1000 / self.step_ms)):
            self.now += self.step_ms
            for camera_id, tracker in self.trackers.items():
                spec = detections_by_camera.get(camera_id, [])
                detections = spec(self.now) if callable(spec) else spec
                active, ended = tracker.update(detections, self.now)
                self.events.extend(self.engine.process(camera_id, self.now, active, ended))
        return self

    def of_type(self, event_type):
        return [event for event in self.events if event.event_type == event_type]
