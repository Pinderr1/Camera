from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FrameFeatures:
    """Per-frame pose summary produced by the camera loop.

    All coordinates are normalized to [0, 1] with y increasing downward.
    `keypoints` are the visible landmark positions used for motion scoring.
    """

    timestamp_ms: int
    person_present: bool
    hip: tuple[float, float] | None = None
    shoulder_mid: tuple[float, float] | None = None
    bbox: tuple[float, float, float, float] | None = None
    keypoints: list[tuple[float, float]] = field(default_factory=list)
    confidence: float = 0.0
    full_body_confidence: float | None = None


def point_in_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    count = len(polygon)
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def torso_angle_deg(shoulder_mid: tuple[float, float], hip: tuple[float, float]) -> float:
    """Angle of the torso from vertical: 0 is upright, 90 is horizontal."""
    dx = abs(shoulder_mid[0] - hip[0])
    dy = abs(shoulder_mid[1] - hip[1])
    return math.degrees(math.atan2(dx, dy))


class PoseEventEngine:
    """Turns per-frame pose features into debounced sensor-event payloads.

    Emits only on state changes so the MQTT topic carries transitions, not a
    frame stream. Zone polygons and thresholds come from the zones config.
    """

    def __init__(self, config: dict[str, Any]):
        self.camera_id = config.get("camera_id", "cam01")
        self.room = config.get("room", "")
        self.floor_line_y = float(config.get("floor_line_y", 0.65))
        self.zones = {zone["id"]: zone for zone in config.get("zones", [])}
        self.thresholds = config.get("events", {})
        self.fall_suppressed_zone_ids = set(self.thresholds.get("fall_suppressed_zone_ids", ["bed_zone"]))

        self._person_present = False
        self._presence_candidate_since: int | None = None
        self._absence_candidate_since: int | None = None
        self._bed_occupied = False
        self._bed_candidate_since: int | None = None
        self._bed_exit_candidate_since: int | None = None
        self._route_motion = False
        self._bathroom_occupied = False
        self._fall_suspected = False
        self._floor_level = False
        self._floor_candidate_since: int | None = None
        self._no_motion = False
        self._last_motion_ms: int | None = None
        self._last_zone: str | None = None
        self._absent_since: int | None = None
        self._hip_history: deque[tuple[int, float]] = deque()
        self._last_keypoints: list[tuple[float, float]] | None = None
        self._last_frame_ms: int | None = None

    def update(self, frame: FrameFeatures) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = frame.timestamp_ms

        motion_score = self._motion_score(frame)
        zone = self._zone_for(frame.hip) if frame.person_present and frame.hip else None
        if zone:
            self._last_zone = zone

        self._update_presence(frame, now, events)

        if self._person_present and frame.hip:
            self._update_bed_occupancy(frame.hip, zone, now, events)
            self._update_route_motion(zone, motion_score, now, events)
            self._update_fall_signals(frame, zone, now, events)
            self._update_no_motion(motion_score, now, events)
        return events

    def _threshold(self, key: str, default: float) -> float:
        return float(self.thresholds.get(key, default))

    def _zone_for(self, hip: tuple[float, float]) -> str | None:
        for zone_id, zone in self.zones.items():
            if point_in_polygon(hip[0], hip[1], zone["polygon"]):
                return zone_id
        return None

    def _update_presence(self, frame: FrameFeatures, now: int, events: list[dict[str, Any]]) -> None:
        if frame.person_present:
            self._absence_candidate_since = None
            if not self._person_present:
                if self._presence_candidate_since is None:
                    self._presence_candidate_since = now
                elif now - self._presence_candidate_since >= self._threshold("person_present_debounce_s", 1.0) * 1000:
                    self._person_present = True
                    self._presence_candidate_since = None
                    self._absent_since = None
                    events.append(self._payload("person_present", True, now, zone_id=self._last_zone or ""))
                    if self._bathroom_occupied:
                        self._bathroom_occupied = False
                        events.append(self._payload("bathroom_occupied", False, now, zone_id="bathroom_door_zone"))
        else:
            self._presence_candidate_since = None
            if self._person_present:
                if self._absence_candidate_since is None:
                    self._absence_candidate_since = now
                elif now - self._absence_candidate_since >= self._threshold("person_absent_debounce_s", 3.0) * 1000:
                    self._person_present = False
                    self._absence_candidate_since = None
                    self._absent_since = now
                    self._reset_motion_state()
                    events.append(self._payload("person_present", False, now, zone_id=self._last_zone or ""))
                    if self._route_motion:
                        self._route_motion = False
                        events.append(self._payload("route_motion", False, now, zone_id="route_zone"))
            elif self._absent_since is not None and not self._bathroom_occupied:
                infer_after_ms = self._threshold("bathroom_infer_absent_s", 5.0) * 1000
                if self._last_zone == "bathroom_door_zone" and now - self._absent_since >= infer_after_ms:
                    self._bathroom_occupied = True
                    events.append(
                        self._payload(
                            "bathroom_occupied",
                            True,
                            now,
                            zone_id="bathroom_door_zone",
                            confidence=self._threshold("bathroom_inferred_confidence", 0.6),
                            notes="inferred: person left through doorway zone",
                        )
                    )

    def _update_bed_occupancy(self, hip: tuple[float, float], zone: str | None, now: int, events: list[dict[str, Any]]) -> None:
        debounce_ms = self._threshold("bed_occupied_debounce_s", 5.0) * 1000
        in_bed = zone == "bed_zone"
        if in_bed:
            self._bed_exit_candidate_since = None
            if not self._bed_occupied:
                if self._bed_candidate_since is None:
                    self._bed_candidate_since = now
                elif now - self._bed_candidate_since >= debounce_ms:
                    self._bed_occupied = True
                    self._bed_candidate_since = None
                    events.append(self._payload("bed_occupied", True, now, zone_id="bed_zone"))
        else:
            self._bed_candidate_since = None
            if self._bed_occupied:
                if self._bed_exit_candidate_since is None:
                    self._bed_exit_candidate_since = now
                elif now - self._bed_exit_candidate_since >= debounce_ms:
                    self._bed_occupied = False
                    self._bed_exit_candidate_since = None
                    events.append(self._payload("bed_occupied", False, now, zone_id="bed_zone"))

    def _update_route_motion(self, zone: str | None, motion_score: float, now: int, events: list[dict[str, Any]]) -> None:
        moving_on_route = zone == "route_zone" and motion_score >= self._threshold("motion_score_threshold", 0.01)
        if moving_on_route != self._route_motion:
            self._route_motion = moving_on_route
            events.append(self._payload("route_motion", moving_on_route, now, zone_id="route_zone"))

    def _update_fall_signals(self, frame: FrameFeatures, zone: str | None, now: int, events: list[dict[str, Any]]) -> None:
        fall_zone_allowed = not self._fall_suppressed_by_zone(frame, zone)
        hip = frame.hip
        assert hip is not None

        self._hip_history.append((now, hip[1]))
        window_ms = self._threshold("rapid_drop_window_s", 0.7) * 1000
        while self._hip_history and now - self._hip_history[0][0] > window_ms:
            self._hip_history.popleft()
        full_body_confidence = frame.full_body_confidence if frame.full_body_confidence is not None else frame.confidence
        full_body_ok = full_body_confidence >= self._threshold("fall_suspected_min_full_body_confidence", 0.65)
        if len(self._hip_history) >= 2 and fall_zone_allowed and full_body_ok and not self._fall_suspected:
            t0, y0 = self._hip_history[0]
            dt_s = (now - t0) / 1000.0
            if dt_s > 0:
                vy = (hip[1] - y0) / dt_s
                if vy >= self._threshold("rapid_drop_min_vy", 0.18):
                    self._fall_suspected = True
                    events.append(
                        self._payload(
                            "fall_suspected",
                            True,
                            now,
                            zone_id=zone or "",
                            confidence=full_body_confidence,
                        )
                    )

        horizontal = False
        if frame.shoulder_mid is not None:
            angle = torso_angle_deg(frame.shoulder_mid, hip)
            horizontal = angle >= self._threshold("floor_level_torso_angle_deg", 55)
        wide_bbox = frame.bbox is not None and frame.bbox[3] > 0 and frame.bbox[2] / frame.bbox[3] > 1.4
        floor_level_now = fall_zone_allowed and hip[1] >= self.floor_line_y and (horizontal or wide_bbox)

        if floor_level_now:
            if self._floor_candidate_since is None:
                self._floor_candidate_since = now
            elif not self._floor_level and now - self._floor_candidate_since >= self._threshold("floor_level_min_s", 2.0) * 1000:
                self._floor_level = True
                events.append(self._payload("floor_level_posture", True, now, zone_id=zone or "", confidence=frame.confidence))
        else:
            self._floor_candidate_since = None
            if self._floor_level:
                self._floor_level = False
                self._fall_suspected = False
                events.append(self._payload("floor_level_posture", False, now, zone_id=zone or ""))

    def _update_no_motion(self, motion_score: float, now: int, events: list[dict[str, Any]]) -> None:
        if motion_score >= self._threshold("motion_score_threshold", 0.01):
            self._last_motion_ms = now
            if self._no_motion:
                self._no_motion = False
                events.append(self._payload("no_motion", False, now, zone_id=self._last_zone or ""))
            return
        if self._last_motion_ms is None:
            self._last_motion_ms = now
            return
        if not self._no_motion and now - self._last_motion_ms >= self._threshold("no_motion_after_s", 10.0) * 1000:
            self._no_motion = True
            events.append(self._payload("no_motion", True, now, zone_id=self._last_zone or ""))

    def _motion_score(self, frame: FrameFeatures) -> float:
        """Mean keypoint displacement per second across consecutive frames."""
        score = 0.0
        if (
            frame.keypoints
            and self._last_keypoints
            and self._last_frame_ms is not None
            and frame.timestamp_ms > self._last_frame_ms
        ):
            count = min(len(frame.keypoints), len(self._last_keypoints))
            if count:
                total = sum(
                    math.hypot(
                        frame.keypoints[i][0] - self._last_keypoints[i][0],
                        frame.keypoints[i][1] - self._last_keypoints[i][1],
                    )
                    for i in range(count)
                )
                dt_s = (frame.timestamp_ms - self._last_frame_ms) / 1000.0
                score = total / count / dt_s
        if frame.person_present:
            self._last_keypoints = frame.keypoints or None
            self._last_frame_ms = frame.timestamp_ms
        else:
            self._last_keypoints = None
            self._last_frame_ms = None
        return score

    def _fall_suppressed_by_zone(self, frame: FrameFeatures, zone: str | None) -> bool:
        if zone in self.fall_suppressed_zone_ids:
            return True
        min_points = int(self.thresholds.get("fall_suppression_min_points", 2))
        for zone_id in self.fall_suppressed_zone_ids:
            zone_config = self.zones.get(zone_id)
            if not zone_config:
                continue
            body_points = []
            if frame.hip:
                body_points.append(frame.hip)
            body_points.extend(frame.keypoints)
            points_in_zone = sum(1 for x, y in body_points if point_in_polygon(x, y, zone_config["polygon"]))
            if points_in_zone >= min_points:
                return True
        return False

    def _reset_motion_state(self) -> None:
        # Losing the pose is not evidence of recovery or of leaving bed:
        # bed occupancy stays as-is (blanket occlusion) and no `no_motion`
        # or `floor_level_posture` clear events are emitted, so a fall
        # escalation in the state machine keeps running while blind.
        self._no_motion = False
        self._last_motion_ms = None
        self._fall_suspected = False
        self._floor_level = False
        self._floor_candidate_since = None
        self._hip_history.clear()
        self._bed_candidate_since = None
        self._bed_exit_candidate_since = None

    def _payload(
        self,
        event_name: str,
        value: bool,
        timestamp_ms: int,
        zone_id: str = "",
        confidence: float = 0.9,
        notes: str = "",
    ) -> dict[str, Any]:
        return {
            "sensor_id": f"pose_{self.camera_id}",
            "sensor_type": "camera_pose",
            "room": self.room,
            "zone_id": zone_id,
            "event_name": event_name,
            "value": value,
            "timestamp_ms": timestamp_ms,
            "confidence": round(confidence, 3),
            "battery_ok": True,
            "network_ok": True,
            "notes": notes,
        }
