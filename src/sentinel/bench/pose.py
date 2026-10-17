"""Offline pose-derived observations, never detector ground truth or live video.

The legacy reader and feature extraction are reused without extending the frozen
senior-safety application. Missing poses and capture gaps remain missing.
"""

from __future__ import annotations

from bisect import bisect_right
import math
from pathlib import Path

from senior_safety.pose_extractor import extract_features
from senior_safety.pose_replay import read_pose_jsonl

from sentinel.models import Detection


class PoseRecordingScenario:
    """Duck-typed input for sentinel.replay.run_scenario; no rendered frames.

    At each replay tick use only the preceding sample, for at most 250 ms.
    Visibility is a pose score, not calibrated object-detector confidence.
    Landmark extents underestimate occluded bodies and cannot locate feet
    reliably; the replay must use an illustrative zone, not a real site map.
    """

    def __init__(self, path: str | Path, camera_id: str = "pose_replay"):
        recording = read_pose_jsonl(path)
        self.name = recording.clip_id
        self.camera_id = camera_id
        self.times = [timestamp - recording.start_ms for timestamp, _ in recording.frames]
        self.duration_s = (recording.duration_ms + 1) / 1000 if recording.frames else 0.0
        self.observations: list[list[Detection]] = []
        for timestamp, landmarks in recording.frames:
            features = extract_features(landmarks, timestamp)
            detection = []
            if features.person_present and features.bbox:
                x, y, w, h = features.bbox
                if all(math.isfinite(v) for v in (x, y, w, h, features.confidence)):
                    x0, y0 = max(0.0, x), max(0.0, y)
                    x1, y1 = min(1.0, x + w), min(1.0, y + h)
                    if x1 > x0 and y1 > y0:
                        detection = [Detection("person", features.confidence, (x0, y0, x1 - x0, y1 - y0))]
            self.observations.append(detection)

    def detections_at(self, camera_id: str, t_s: float) -> list[Detection]:
        if camera_id != self.camera_id or t_s < 0 or t_s >= self.duration_s:
            return []
        now = round(t_s * 1000)
        index = bisect_right(self.times, now) - 1
        if index < 0 or now - self.times[index] > 250:
            return []
        return list(self.observations[index])
