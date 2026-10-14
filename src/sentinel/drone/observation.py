"""What the drone camera saw during an inspection.

A moving camera has no fixed geometry, so drone detections never enter zone
rules. They become *inspection observations*: what was visible, when, in how
many frames, with an approximate ground position only when the camera pose
supports one. Each estimate says how it was made and how uncertain it is; when
it cannot be made, the reason is stated instead.

Ground positions assume flat ground at the take-off elevation and a pinhole
camera. Nothing here links a drone observation to a person or vehicle seen by
a fixed camera: they may or may not be the same. Standard library only."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from ..incidents import DISCLAIMER
from ..models import Detection
from ..tracker import IoUTracker, TrackerConfig
from .base import distance_m, geo_to_offset, offset_to_geo

MAX_POSE_AGE_S = 1.0
MIN_DEPRESSION_DEG = 12.0  # rays flatter than this hit the ground too far away to estimate
MAX_RANGE_FACTOR = 5.0  # no estimate beyond 5x the height above ground


@dataclass
class CameraModel:
    hfov_deg: float = 80.0
    vfov_deg: float = 50.0
    default_pitch_deg: float = -90.0  # used when the gimbal does not report its angle
    assumed_attitude_error_deg: float = 6.0  # gimbal angle not reported
    reported_attitude_error_deg: float = 2.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "CameraModel":
        data = data or {}
        return cls(**{key: float(data[key]) for key in cls.__dataclass_fields__ if key in data})


@dataclass
class CameraPose:
    lat: float
    lon: float
    height_m: float  # above the take-off point
    heading_deg: float
    pitch_deg: float  # camera pitch, -90 = straight down
    yaw_offset_deg: float = 0.0  # camera yaw relative to the aircraft heading
    attitude_source: str = "configured"  # gimbal_reported | configured
    position_error_m: float = 2.5
    age_s: float = 0.0


@dataclass
class GroundEstimate:
    lat: float | None = None
    lon: float | None = None
    uncertainty_m: float | None = None
    method: str = ""
    reason: str = ""  # why no estimate was made

    @property
    def available(self) -> bool:
        return self.lat is not None


def pose_from_telemetry(telemetry: Any, camera: CameraModel, now_ms: int) -> CameraPose | None:
    if telemetry.lat is None or telemetry.lon is None:
        return None
    reported = telemetry.gimbal_pitch_deg is not None
    sample_ms = telemetry.telemetry_ms or now_ms
    error = 2.5 if telemetry.hdop is None else max(1.0, 2.5 * float(telemetry.hdop))
    return CameraPose(
        lat=telemetry.lat, lon=telemetry.lon, height_m=float(telemetry.alt_m), heading_deg=float(telemetry.heading_deg or 0.0),
        pitch_deg=float(telemetry.gimbal_pitch_deg) if reported else camera.default_pitch_deg,
        yaw_offset_deg=float(telemetry.gimbal_yaw_deg or 0.0) if reported else 0.0,
        attitude_source="gimbal_reported" if reported else "configured", position_error_m=error,
        age_s=max(0.0, (now_ms - sample_ms) / 1000),
    )


def _axes(pose: CameraPose) -> tuple[tuple[float, float, float], ...]:
    """Forward, right and down unit vectors in east-north-up coordinates."""
    yaw = math.radians(pose.heading_deg + pose.yaw_offset_deg)
    pitch = math.radians(pose.pitch_deg)
    forward = (math.cos(pitch) * math.sin(yaw), math.cos(pitch) * math.cos(yaw), math.sin(pitch))
    if abs(math.cos(pitch)) < 1e-6:  # straight down: the top of the image points along the heading
        forward = (math.sin(yaw) * 1e-6, math.cos(yaw) * 1e-6, -1.0)
    right = (math.cos(yaw), -math.sin(yaw), 0.0)
    up = (right[1] * forward[2] - right[2] * forward[1], right[2] * forward[0] - right[0] * forward[2], right[0] * forward[1] - right[1] * forward[0])
    norm = math.sqrt(sum(c * c for c in up)) or 1.0
    down = tuple(-c / norm for c in up)
    return forward, right, down


def image_to_ground(pose: CameraPose, camera: CameraModel, u: float, v: float) -> GroundEstimate:
    """Normalised image point (0..1, origin top-left) to a ground position."""
    if pose.age_s > MAX_POSE_AGE_S:
        return GroundEstimate(reason=f"camera position is {pose.age_s:.1f} s old")
    if pose.height_m < 2.0:
        return GroundEstimate(reason="aircraft too low for a position estimate")
    forward, right, down = _axes(pose)
    x = math.tan(math.radians(camera.hfov_deg) / 2) * (2 * u - 1)
    y = math.tan(math.radians(camera.vfov_deg) / 2) * (2 * v - 1)
    ray = tuple(forward[i] + x * right[i] + y * down[i] for i in range(3))
    length = math.sqrt(sum(c * c for c in ray))
    ray = tuple(c / length for c in ray)
    depression = math.degrees(math.asin(max(-1.0, min(1.0, -ray[2]))))
    if depression < MIN_DEPRESSION_DEG:
        return GroundEstimate(reason="camera looks too close to the horizon for a ground position")
    distance = pose.height_m / math.tan(math.radians(depression)) if depression < 89.99 else 0.0
    if distance > MAX_RANGE_FACTOR * pose.height_m:
        return GroundEstimate(reason="object too far from the aircraft for a reliable position")
    t = pose.height_m / -ray[2]
    east, north = ray[0] * t, ray[1] * t
    lat, lon = offset_to_geo((pose.lat, pose.lon), east, north)
    slant = math.sqrt(distance ** 2 + pose.height_m ** 2)
    attitude_error = camera.reported_attitude_error_deg if pose.attitude_source == "gimbal_reported" else camera.assumed_attitude_error_deg
    angular = slant * math.radians(attitude_error) / max(math.sin(math.radians(depression)), 0.2)
    height_error = 1.0 * distance / pose.height_m  # barometric height error of about 1 m stretches oblique rays
    uncertainty = math.sqrt(pose.position_error_m ** 2 + angular ** 2 + height_error ** 2)
    method = ("camera angle reported by the gimbal" if pose.attitude_source == "gimbal_reported"
              else f"camera angle assumed from configuration ({camera.default_pitch_deg:g}°)") + "; assumes flat ground"
    return GroundEstimate(lat=lat, lon=lon, uncertainty_m=round(uncertainty, 1), method=method)


def ground_to_image(pose: CameraPose, camera: CameraModel, lat: float, lon: float, up_m: float = 0.0) -> tuple[float, float] | None:
    """Inverse projection (used by the simulated aerial camera and tests)."""
    east, north = geo_to_offset((pose.lat, pose.lon), lat, lon)
    vector = (east, north, up_m - pose.height_m)
    forward, right, down = _axes(pose)
    depth = sum(vector[i] * forward[i] for i in range(3))
    if depth <= 0.1:
        return None
    x = sum(vector[i] * right[i] for i in range(3)) / depth
    y = sum(vector[i] * down[i] for i in range(3)) / depth
    u = 0.5 + x / (2 * math.tan(math.radians(camera.hfov_deg) / 2))
    v = 0.5 + y / (2 * math.tan(math.radians(camera.vfov_deg) / 2))
    return u, v


@dataclass
class Observation:
    observation_id: str
    label: str
    kind: str
    first_ms: int
    last_ms: int
    frames: int = 0
    max_confidence: float = 0.0
    estimates: list[tuple[float, float, float]] = field(default_factory=list)  # (lat, lon, uncertainty)
    no_estimate_reasons: dict[str, int] = field(default_factory=dict)
    snapshot_path: str = ""
    best_frame_ms: int = 0

    def position(self) -> dict[str, Any]:
        if not self.estimates:
            reason = max(self.no_estimate_reasons, key=self.no_estimate_reasons.get) if self.no_estimate_reasons else "no camera position"
            return {"available": False, "reason": reason}
        weights = [1 / max(u, 0.5) ** 2 for _, _, u in self.estimates]
        total = sum(weights)
        lat = sum(w * e[0] for w, e in zip(weights, self.estimates)) / total
        lon = sum(w * e[1] for w, e in zip(weights, self.estimates)) / total
        best = min(u for _, _, u in self.estimates)
        spread = max(distance_m((lat, lon), (e[0], e[1])) for e in self.estimates)
        return {"available": True, "lat": lat, "lon": lon, "uncertainty_m": round(max(best, spread), 1), "samples": len(self.estimates)}


class InspectionObserver:
    """Tracks detections across consecutive drone frames for one inspection.

    Image-space tracking holds while the aircraft hovers. When the camera
    moves, one object can split into several tracks, so summaries report the
    most objects visible *at the same time* (a lower bound) next to the track
    count (which may over-count)."""

    def __init__(self, request_id: str, target: tuple[float, float], target_label: str, camera: CameraModel,
                 origin: tuple[float, float], *, min_frames: int = 2) -> None:
        self.request_id = request_id
        self.target = target
        self.target_label = target_label
        self.camera = camera
        self.origin = origin
        self.tracker = IoUTracker(f"drone:{request_id}", TrackerConfig(min_hits=min_frames, max_missed_ms=2000))
        self.observations: dict[str, Observation] = {}
        self.max_visible: dict[str, int] = {}
        self.frames = 0
        self.frames_with_pose = 0
        self.first_ms = 0
        self.last_ms = 0
        self.pose_sources: set[str] = set()

    def update(self, detections: list[Detection], frame_ms: int, pose: CameraPose | None) -> list[Observation]:
        """Returns observations confirmed in this frame (the caller saves a snapshot for each)."""
        self.frames += 1
        self.first_ms = self.first_ms or frame_ms
        self.last_ms = frame_ms
        if pose is not None and pose.age_s <= MAX_POSE_AGE_S:
            self.frames_with_pose += 1
            self.pose_sources.add(pose.attitude_source)
        active, _ = self.tracker.update(detections, frame_ms)
        counts: dict[str, int] = {}
        new = []
        for track in active:
            if not track.confirmed:
                continue
            counts[track.label] = counts.get(track.label, 0) + 1
            observation = self.observations.get(track.track_id)
            if observation is None:
                # Sequential ids: tracker ids contain ":" which is not a valid file-name character on Windows.
                observation = Observation(f"{self.request_id}_obs{len(self.observations) + 1}", track.label, track.kind or track.label, frame_ms, frame_ms)
                self.observations[track.track_id] = observation
                new.append(observation)
            observation.last_ms = frame_ms
            observation.frames += 1
            if track.confidence >= observation.max_confidence:
                observation.max_confidence = round(track.confidence, 3)
                observation.best_frame_ms = frame_ms
            if pose is None:
                observation.no_estimate_reasons["no camera position"] = observation.no_estimate_reasons.get("no camera position", 0) + 1
                continue
            x, y, w, h = track.bbox
            nadir = pose.pitch_deg <= -70
            u, v = (x + w / 2, y + h / 2) if nadir else (x + w / 2, y + h)  # from above, the box centre; obliquely, the feet
            estimate = image_to_ground(pose, self.camera, u, v)
            if estimate.available:
                observation.estimates.append((estimate.lat, estimate.lon, estimate.uncertainty_m))
                del observation.estimates[:-50]
            else:
                observation.no_estimate_reasons[estimate.reason] = observation.no_estimate_reasons.get(estimate.reason, 0) + 1
        for label, count in counts.items():
            self.max_visible[label] = max(self.max_visible.get(label, 0), count)
        return new

    def summary(self) -> dict[str, Any]:
        rows = []
        for observation in sorted(self.observations.values(), key=lambda o: o.first_ms):
            position = observation.position()
            if position["available"]:
                gap = distance_m(self.target, (position["lat"], position["lon"]))
                east, north = geo_to_offset(self.origin, position["lat"], position["lon"])
                position |= {"distance_from_target_m": round(gap, 1), "map": [round(east, 1), round(-north, 1)]}
            rows.append({"observation_id": observation.observation_id, "label": observation.label, "kind": observation.kind,
                         "first_ms": observation.first_ms, "last_ms": observation.last_ms, "frames": observation.frames,
                         "max_confidence": observation.max_confidence, "position": position, "snapshot_path": observation.snapshot_path})
        return {
            "request_id": self.request_id, "target_label": self.target_label, "frames": self.frames, "frames_with_pose": self.frames_with_pose,
            "first_ms": self.first_ms, "last_ms": self.last_ms, "max_visible": dict(self.max_visible), "tracks": len(rows),
            "observations": rows, "pose_sources": sorted(self.pose_sources), "text": describe(self.target_label, rows, dict(self.max_visible), self.frames, self.pose_sources),
        }


LABEL_NAMES = {"person": "Person", "vehicle": "Vehicle", "animal": "Animal", "bicycle": "Bicycle"}


def describe(target_label: str, rows: list[dict[str, Any]], max_visible: dict[str, int], frames: int, pose_sources: set[str]) -> list[str]:
    """Plain sentences: what was visible, how sure, and what cannot be concluded."""
    place = target_label or "the inspection point"
    if not frames:
        return [f"No drone video was analysed for the inspection of {place}.", DISCLAIMER]
    if not rows:
        return [f"The drone camera analysed {frames} frame(s) over {place} and did not detect any person, vehicle or animal. "
                "This does not prove the area was empty: small, hidden or poorly lit objects can be missed.", DISCLAIMER]
    lines = []
    for label, count in sorted(max_visible.items()):
        name = LABEL_NAMES.get(label, label.capitalize())
        tracks = [row for row in rows if row["label"] == label]
        seen = sum(row["frames"] for row in tracks)
        confidence = max(row["max_confidence"] for row in tracks)
        lines.append(f"{name}: up to {count} visible at the same time near {place}, detected in {seen} frame(s) "
                     f"(highest detector confidence {round(confidence * 100)}%).")
        placed = [row for row in tracks if row["position"].get("available")]
        if placed:
            best = min(placed, key=lambda row: row["position"]["uncertainty_m"])
            lines.append(f"  Estimated position: about {best['position']['distance_from_target_m']:g} m from the inspection point, "
                         f"uncertain by about ±{best['position']['uncertainty_m']:g} m.")
        else:
            reasons = {row["position"].get("reason", "") for row in tracks}
            lines.append(f"  No ground position estimated ({'; '.join(sorted(r for r in reasons if r))}).")
    if "configured" in pose_sources:
        lines.append("Positions use the configured camera angle because the gimbal did not report its own; they assume flat ground.")
    lines.append("Drone observations are not linked to people or vehicles seen by the fixed cameras: they may or may not be the same.")
    lines.append(DISCLAIMER)
    return lines


def observation_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in ("observation_id", "label", "kind", "first_ms", "last_ms", "frames", "max_confidence", "position", "snapshot_path") if key in data}


__all__ = ["CameraModel", "CameraPose", "GroundEstimate", "InspectionObserver", "Observation", "asdict", "describe", "ground_to_image",
           "image_to_ground", "pose_from_telemetry"]
