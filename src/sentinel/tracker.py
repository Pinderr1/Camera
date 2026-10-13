from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .models import BBox, Detection, Track, canonical_class


def iou(a: BBox, b: BBox) -> float:
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    inter_w = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    inter_h = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = inter_w * inter_h
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _center(box: BBox) -> tuple[float, float]:
    return (box[0] + box[2] / 2, box[1] + box[3] / 2)


@dataclass
class TrackerConfig:
    iou_threshold: float = 0.25
    # Fallback match for fast movers whose boxes stop overlapping between
    # detector runs: centre distance in units of the track's box diagonal.
    max_center_distance: float = 1.2
    min_hits: int = 3
    max_missed_ms: int = 2500
    smoothing: float = 0.6
    # ByteTrack-style second stage: detections below this score may only extend
    # an existing track (IoU match), never start one. 0 disables the split.
    high_score: float = 0.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "TrackerConfig":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class IoUTracker:
    """Per-camera multi-object tracker: constant-velocity prediction plus greedy
    IoU / centre-distance association, with an optional ByteTrack-style second
    pass that lets low-confidence boxes (partly occluded people) keep existing
    tracks alive without ever creating new ones. Dependency-free; the idea, not
    the code, comes from ByteTrack (Zhang et al., 2022)."""

    def __init__(self, camera_id: str, config: TrackerConfig | None = None) -> None:
        self.camera_id = camera_id
        self.config = config or TrackerConfig()
        self.tracks: dict[str, Track] = {}
        self._next_id = 1

    def update(self, detections: Iterable[Detection], timestamp_ms: int) -> tuple[list[Track], list[Track]]:
        """Returns (confirmed active tracks, tracks that ended this update)."""
        detections = [
            Detection(canonical_class(det.label), det.confidence, det.bbox) for det in detections
        ]
        high = [det for det in detections if det.confidence >= self.config.high_score]
        low = [det for det in detections if det.confidence < self.config.high_score]
        predicted = {track_id: self._predict(track, timestamp_ms) for track_id, track in self.tracks.items()}

        matched_tracks: set[str] = set()
        matched_high = self._associate(high, predicted, matched_tracks, timestamp_ms, allow_distance=True)
        if low:
            self._associate(low, predicted, matched_tracks, timestamp_ms, allow_distance=False)

        for index, det in enumerate(high):
            if index not in matched_high:
                self._spawn(det, timestamp_ms)

        ended: list[Track] = []
        for track_id in list(self.tracks):
            track = self.tracks[track_id]
            if track_id in matched_tracks or track.last_seen_ms == timestamp_ms:
                continue
            track.missed_frames += 1
            if timestamp_ms - track.last_seen_ms > self.config.max_missed_ms:
                del self.tracks[track_id]
                if track.confirmed:
                    ended.append(track)

        active = [track for track in self.tracks.values() if track.confirmed and track.last_seen_ms == timestamp_ms]
        return active, ended

    def _associate(
        self,
        detections: list[Detection],
        predicted: dict[str, BBox],
        matched_tracks: set[str],
        timestamp_ms: int,
        *,
        allow_distance: bool,
    ) -> set[int]:
        pairs: list[tuple[float, str, int]] = []
        for track_id, track in self.tracks.items():
            if track_id in matched_tracks or track_id not in predicted:
                continue
            box = predicted[track_id]
            diagonal = math.hypot(box[2], box[3]) or 1e-6
            for index, det in enumerate(detections):
                if det.label != track.label:
                    continue
                overlap = iou(box, det.bbox)
                if overlap >= self.config.iou_threshold:
                    pairs.append((1.0 + overlap, track_id, index))
                    continue
                if not allow_distance:
                    continue
                cx, cy = _center(box)
                dx, dy = _center(det.bbox)
                distance = math.hypot(cx - dx, cy - dy) / diagonal
                if distance <= self.config.max_center_distance:
                    pairs.append((1.0 - distance / (self.config.max_center_distance * 2), track_id, index))
        matched_dets: set[int] = set()
        for _, track_id, index in sorted(pairs, reverse=True):
            if track_id in matched_tracks or index in matched_dets:
                continue
            matched_tracks.add(track_id)
            matched_dets.add(index)
            self._apply(self.tracks[track_id], detections[index], timestamp_ms)
        return matched_dets

    def flush(self) -> list[Track]:
        ended = [track for track in self.tracks.values() if track.confirmed]
        self.tracks.clear()
        return ended

    def _predict(self, track: Track, timestamp_ms: int) -> BBox:
        dt = max(0.0, (timestamp_ms - track.last_seen_ms) / 1000.0)
        x, y, w, h = track.bbox
        return (x + track.velocity[0] * dt, y + track.velocity[1] * dt, w, h)

    def _apply(self, track: Track, det: Detection, timestamp_ms: int) -> None:
        dt = (timestamp_ms - track.last_seen_ms) / 1000.0
        if dt > 0:
            alpha = self.config.smoothing
            vx = (det.bbox[0] - track.bbox[0]) / dt
            vy = (det.bbox[1] - track.bbox[1]) / dt
            track.velocity = (
                alpha * track.velocity[0] + (1 - alpha) * vx,
                alpha * track.velocity[1] + (1 - alpha) * vy,
            )
        track.bbox = det.bbox
        track.confidence = 0.7 * track.confidence + 0.3 * det.confidence
        track.last_seen_ms = timestamp_ms
        track.hits += 1
        track.missed_frames = 0
        if track.hits >= self.config.min_hits:
            track.confirmed = True
        anchor = track.anchor
        track.max_displacement = max(
            track.max_displacement,
            math.hypot(anchor[0] - track.first_anchor[0], anchor[1] - track.first_anchor[1]),
        )
        track.history.append((timestamp_ms, anchor))

    def _spawn(self, det: Detection, timestamp_ms: int) -> None:
        track_id = f"{self.camera_id}:{self._next_id}"
        self._next_id += 1
        track = Track(
            track_id=track_id,
            camera_id=self.camera_id,
            label=det.label,
            bbox=det.bbox,
            confidence=det.confidence,
            first_seen_ms=timestamp_ms,
            last_seen_ms=timestamp_ms,
            confirmed=self.config.min_hits <= 1,
            first_anchor=det.anchor,
        )
        track.history.append((timestamp_ms, det.anchor))
        self.tracks[track_id] = track
