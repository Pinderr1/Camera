from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FrameFeatures:
    """Per-frame pose summary produced by the camera loop.

    All coordinates are normalized to [0, 1] with y increasing downward.
    `keypoints` maps landmark names to visible positions so motion scoring
    pairs the same body part across frames. `torso_length` is the
    shoulder-to-hip distance used as the body-size scale, and `ankle_y` is
    the lowest visible ankle (closest to the floor).
    """

    timestamp_ms: int
    person_present: bool
    hip: tuple[float, float] | None = None
    shoulder_mid: tuple[float, float] | None = None
    bbox: tuple[float, float, float, float] | None = None
    keypoints: dict[str, tuple[float, float]] = field(default_factory=dict)
    confidence: float = 0.0
    full_body_confidence: float | None = None
    torso_length: float | None = None
    ankle_y: float | None = None


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

    Robustness model: landmark positions are EMA-smoothed with hip-glitch
    rejection, motion thresholds are expressed in body-lengths per second so
    they hold at any camera distance, a rapid drop only becomes
    `fall_suspected` once posture corroborates it, and floor-level posture
    latches through counted hysteresis instead of single frames.
    """

    def __init__(self, config: dict[str, Any]):
        self.camera_id = config.get("camera_id", "cam01")
        self.room = config.get("room", "")
        self.zones = {zone["id"]: zone for zone in config.get("zones", [])}
        self.thresholds = config.get("events", {})
        self.calibration = config.get("calibration", {})
        self.floor_line_y = float(self.calibration.get("floor_line_y", config.get("floor_line_y", 0.65)))
        self.fall_suppressed_zone_ids = set(self.thresholds.get("fall_suppressed_zone_ids", []))
        self.fall_suppressed_zone_ids.update(
            zone_id for zone_id, zone in self.zones.items() if zone.get("suppress_fall_alerts") is True
        )

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
        self._no_motion = False
        self._last_motion_ms: int | None = None
        self._motion_streak = 0
        self._last_zone: str | None = None
        self._effective_zone: str | None = None
        self._zone_candidate: str | None = None
        self._zone_candidate_since: int | None = None
        self._absent_since: int | None = None
        self._evidence_wiped = False
        self._drop_pending_since: int | None = None
        self._rapid_drop_confirmed = False
        self._hip_history: deque[tuple[int, float]] = deque()
        self._floor_history: deque[tuple[int, bool]] = deque()
        self._torso_history: deque[tuple[int, float]] = deque()
        self._upright_bbox_history: deque[tuple[int, float]] = deque()
        self._smoothed: dict[str, tuple[float, float]] = {}
        self._smoothed_bbox: tuple[float, float, float, float] | None = None
        self._last_smooth_ms: int | None = None
        self._last_discontinuity_ms: int | None = None
        self._glitch_streak = 0
        self._prev_keypoints: dict[str, tuple[float, float]] | None = None
        self._prev_keypoints_ms: int | None = None
        self._debug = {
            "current_zone": "",
            "zone_type": "unknown",
            "full_body_pose_confidence": 0.0,
            "fall_suspected": False,
            "floor_level": False,
            "no_motion": False,
        }

    def update(self, frame: FrameFeatures) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        now = frame.timestamp_ms

        hip, shoulder_mid, bbox = self._smooth_frame(frame)
        motion_score = self._motion_score_bl(frame)
        self._update_body_model(hip, shoulder_mid, bbox, now, frame.person_present)
        current_zone = self._zone_for(hip) if frame.person_present and hip else None
        zone = self._update_effective_zone(hip if frame.person_present else None, now)
        if zone:
            self._last_zone = zone

        self._update_presence(frame, now, events)

        if self._person_present and hip:
            self._update_bed_occupancy(zone, now, events)
            self._update_route_motion(zone, motion_score, now, events)
            zone_type = self._zone_type(current_zone)
            fall_suppressed = self._fall_suppressed_by_zone(frame, hip, current_zone)
            if fall_suppressed:
                self._clear_fall_evidence(current_zone, now, events)
            else:
                if zone_type == "neutral":
                    self._clear_fall_evidence(current_zone, now, events)
                self._update_fall_signals(
                    frame, hip, shoulder_mid, bbox, current_zone, zone_type, now, events
                )
                evidence_allowed = zone_type == "fall_risk" or (
                    zone_type == "unknown" and self._rapid_drop_confirmed
                )
                self._update_no_motion(
                    motion_score, current_zone, now, events, evidence_allowed, self._full_body_confidence(frame)
                )
        self._debug = {
            "current_zone": current_zone or "",
            "zone_type": self._zone_type(current_zone),
            "full_body_pose_confidence": round(self._full_body_confidence(frame), 3),
            "fall_suspected": self._fall_suspected,
            "floor_level": self._floor_level,
            "no_motion": self._no_motion,
        }
        return events

    def diagnostics(self) -> dict[str, Any]:
        """Current conservative fall-gating state for the preview overlay."""
        return dict(self._debug)

    def _threshold(self, key: str, default: float) -> float:
        return float(self.thresholds.get(key, default))

    @staticmethod
    def _full_body_confidence(frame: FrameFeatures) -> float:
        return frame.full_body_confidence if frame.full_body_confidence is not None else frame.confidence

    # ------------------------------------------------------------------
    # Smoothing and body model

    def _smooth_frame(
        self, frame: FrameFeatures
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None, tuple[float, float, float, float] | None]:
        now = frame.timestamp_ms
        if not frame.person_present or frame.hip is None:
            return frame.hip, frame.shoulder_mid, frame.bbox

        gap_s = (now - self._last_smooth_ms) / 1000.0 if self._last_smooth_ms is not None else None
        if gap_s is None or gap_s <= 0 or gap_s > 1.0:
            self._mark_track_discontinuity(frame.hip, now)
            if frame.shoulder_mid:
                self._smoothed["shoulder_mid"] = frame.shoulder_mid
            self._smoothed_bbox = frame.bbox
            self._last_smooth_ms = now
            return frame.hip, frame.shoulder_mid, self._smoothed_bbox

        tau = self._threshold("smoothing_tau_s", 0.15)
        alpha = 1.0 - math.exp(-gap_s / max(tau, 1e-3))
        hip = self._smooth_hip(frame.hip, alpha, now)
        shoulder_mid = frame.shoulder_mid
        if shoulder_mid is not None:
            previous = self._smoothed.get("shoulder_mid")
            if previous is not None:
                shoulder_mid = (
                    previous[0] + alpha * (shoulder_mid[0] - previous[0]),
                    previous[1] + alpha * (shoulder_mid[1] - previous[1]),
                )
            self._smoothed["shoulder_mid"] = shoulder_mid
        bbox = frame.bbox
        if bbox is not None:
            previous_bbox = self._smoothed_bbox
            if previous_bbox is not None:
                bbox = tuple(previous_bbox[i] + alpha * (bbox[i] - previous_bbox[i]) for i in range(4))
            self._smoothed_bbox = bbox
        self._last_smooth_ms = now
        return hip, shoulder_mid, bbox

    def _mark_track_discontinuity(self, hip: tuple[float, float], now: int | None = None) -> None:
        # A teleporting or re-acquired track is a discontinuity, not motion:
        # drop pre-discontinuity history so it cannot register as a drop, and
        # let drop detection re-arm only after the track proves stable.
        self._smoothed = {"hip": hip}
        self._smoothed_bbox = None
        self._hip_history.clear()
        self._drop_pending_since = None
        self._glitch_streak = 0
        self._last_discontinuity_ms = now if now is not None else self._last_smooth_ms

    def _smooth_hip(self, raw: tuple[float, float], alpha: float, now: int) -> tuple[float, float]:
        previous = self._smoothed.get("hip")
        if previous is None:
            self._smoothed["hip"] = raw
            return raw
        jump = math.hypot(raw[0] - previous[0], raw[1] - previous[1])
        if jump > self._threshold("outlier_jump_bl", 1.5) * self._body_scale():
            self._glitch_streak += 1
            if self._glitch_streak < int(self._threshold("outlier_accept_frames", 5)):
                # Nonphysical teleport (landmark mistrack): hold the last
                # believable position unless it persists.
                return previous
            self._mark_track_discontinuity(raw, now)
            return raw
        self._glitch_streak = 0
        smoothed = (previous[0] + alpha * (raw[0] - previous[0]), previous[1] + alpha * (raw[1] - previous[1]))
        self._smoothed["hip"] = smoothed
        return smoothed

    def _update_body_model(
        self,
        hip: tuple[float, float] | None,
        shoulder_mid: tuple[float, float] | None,
        bbox: tuple[float, float, float, float] | None,
        now: int,
        present: bool,
    ) -> None:
        if not present or hip is None or shoulder_mid is None:
            return
        torso = math.hypot(shoulder_mid[0] - hip[0], shoulder_mid[1] - hip[1])
        self._torso_history.append((now, torso))
        while self._torso_history and now - self._torso_history[0][0] > 2000:
            self._torso_history.popleft()
        if bbox is not None and torso_angle_deg(shoulder_mid, hip) < 30:
            self._upright_bbox_history.append((now, bbox[3]))
            while self._upright_bbox_history and now - self._upright_bbox_history[0][0] > 120_000:
                self._upright_bbox_history.popleft()

    def _body_scale(self) -> float:
        if self._torso_history:
            values = sorted(value for _, value in self._torso_history)
            scale = values[len(values) // 2]
        elif self._smoothed_bbox is not None and self._smoothed_bbox[3] > 0:
            scale = 0.35 * self._smoothed_bbox[3]
        else:
            scale = float(self.calibration.get("torso_length_norm", 0.25))
        return max(0.05, scale)

    def _standing_bbox_h(self) -> float | None:
        if len(self._upright_bbox_history) >= 10:
            values = sorted(value for _, value in self._upright_bbox_history)
            return values[round(0.9 * (len(values) - 1))]
        calibrated = self.calibration.get("standing_bbox_h")
        return float(calibrated) if calibrated else None

    # ------------------------------------------------------------------
    # Zones

    def _zone_for(self, hip: tuple[float, float]) -> str | None:
        matches = [
            zone_id
            for zone_id, zone in self.zones.items()
            if point_in_polygon(hip[0], hip[1], zone["polygon"])
        ]
        # Overlapping placeholders are common. Safe-rest wins so a bed/sofa
        # can never be shadowed by a larger floor polygon; fall-risk then wins
        # over neutral route/doorway overlays.
        for preferred in ("safe_rest", "fall_risk", "neutral"):
            for zone_id in matches:
                if self._zone_type(zone_id) == preferred:
                    return zone_id
        return None

    def _zone_type(self, zone_id: str | None) -> str:
        if not zone_id or zone_id not in self.zones:
            return "unknown"
        zone = self.zones[zone_id]
        explicit = zone.get("zone_type")
        if explicit in {"safe_rest", "fall_risk", "neutral"}:
            return explicit
        # Compatibility for existing household configs while they are migrated.
        if zone.get("suppress_fall_alerts") is True:
            return "safe_rest"
        if zone.get("kind") == "floor":
            return "fall_risk"
        return "neutral"

    def _update_effective_zone(self, hip: tuple[float, float] | None, now: int) -> str | None:
        instantaneous = self._zone_for(hip) if hip else None
        if instantaneous == self._effective_zone:
            self._zone_candidate = None
            return self._effective_zone
        if instantaneous != self._zone_candidate:
            self._zone_candidate = instantaneous
            self._zone_candidate_since = now
            return self._effective_zone
        if self._zone_candidate_since is not None and now - self._zone_candidate_since >= self._threshold(
            "zone_hysteresis_s", 0.7
        ) * 1000:
            self._effective_zone = instantaneous
            self._zone_candidate = None
        return self._effective_zone

    # ------------------------------------------------------------------
    # Presence

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
                    self._evidence_wiped = False
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
                    self._reset_tracking_state()
                    events.append(self._payload("person_present", False, now, zone_id=self._last_zone or ""))
                    if self._route_motion:
                        self._route_motion = False
                        events.append(self._payload("route_motion", False, now, zone_id="route_zone"))
            elif self._absent_since is not None:
                if not self._bathroom_occupied:
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
                if not self._evidence_wiped and now - self._absent_since >= self._threshold(
                    "evidence_retention_s", 30.0
                ) * 1000:
                    # Long absence: silently drop stale incident evidence so a
                    # reappearing person needs fresh evidence, while the state
                    # machine keeps its own timers running blind.
                    self._wipe_incident_evidence()

    # ------------------------------------------------------------------
    # Bed / route

    def _update_bed_occupancy(self, zone: str | None, now: int, events: list[dict[str, Any]]) -> None:
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
        moving_on_route = zone == "route_zone" and motion_score >= self._threshold("motion_score_threshold_bl", 0.15)
        if moving_on_route != self._route_motion:
            self._route_motion = moving_on_route
            events.append(self._payload("route_motion", moving_on_route, now, zone_id="route_zone"))

    # ------------------------------------------------------------------
    # Fall signals

    def _update_fall_signals(
        self,
        frame: FrameFeatures,
        hip: tuple[float, float],
        shoulder_mid: tuple[float, float] | None,
        bbox: tuple[float, float, float, float] | None,
        zone: str | None,
        zone_type: str,
        now: int,
        events: list[dict[str, Any]],
    ) -> None:
        full_body = self._full_body_confidence(frame)
        high_confidence = full_body >= self._threshold("fall_suspected_min_full_body_confidence", 0.65)
        drop_zone_allowed = zone_type in {"fall_risk", "unknown"}
        scale = self._body_scale()

        if not 0.0 <= hip[1] <= 1.02:
            # Hip outside the visible frame (MediaPipe extrapolates with high
            # visibility): a person cropped by the frame edge is unknown, not
            # fallen. Hold all fall evidence rather than accumulate or clear.
            self._drop_pending_since = None
            return

        self._hip_history.append((now, hip[1]))
        window_ms = self._threshold("rapid_drop_window_s", 0.8) * 1000
        while self._hip_history and now - self._hip_history[0][0] > window_ms:
            self._hip_history.popleft()

        track_stable = (
            self._last_discontinuity_ms is None
            or now - self._last_discontinuity_ms >= self._threshold("track_stabilize_s", 1.5) * 1000
        )
        if (
            drop_zone_allowed
            and high_confidence
            and track_stable
            and not self._fall_suspected
            and self._drop_pending_since is None
            and len(self._hip_history) >= 3
        ):
            top_ts, top_y = min(self._hip_history, key=lambda item: item[1])
            span_s = (now - top_ts) / 1000.0
            drop = hip[1] - top_y
            if (
                span_s >= self._threshold("rapid_drop_min_span_s", 0.4)
                and drop >= self._threshold("rapid_drop_min_total_bl", 0.7) * scale
                and drop / span_s >= self._threshold("rapid_drop_min_vy_bl", 1.2) * scale
            ):
                self._drop_pending_since = now

        angle = torso_angle_deg(shoulder_mid, hip) if shoulder_mid is not None else None
        horizontal, collapsed, low = self._posture_cues(hip, angle, bbox, frame.ankle_y, scale)
        # Floor level requires the body to actually be low in the scene plus a
        # posture cue; horizontal+collapsed alone matches a person seated close
        # to the camera. Drop corroboration likewise requires ending up low or
        # collapsed — a bend or crouch is horizontal but never low.
        floor_level_now = low and (horizontal or collapsed)
        corroborated = collapsed or floor_level_now

        if self._drop_pending_since is not None:
            if not drop_zone_allowed or not high_confidence:
                self._drop_pending_since = None
            elif corroborated:
                self._drop_pending_since = None
                self._fall_suspected = True
                self._rapid_drop_confirmed = True
                events.append(
                    self._payload("fall_suspected", True, now, zone_id=zone or "", confidence=full_body)
                )
            elif now - self._drop_pending_since > self._threshold("fall_corroborate_window_s", 1.5) * 1000:
                self._drop_pending_since = None

        floor_allowed = high_confidence and (
            zone_type == "fall_risk" or (zone_type == "unknown" and self._rapid_drop_confirmed)
        )
        self._update_floor_latch(floor_allowed and floor_level_now, full_body, zone, now, events)

    def _posture_cues(
        self,
        hip: tuple[float, float],
        angle: float | None,
        bbox: tuple[float, float, float, float] | None,
        ankle_y: float | None,
        scale: float,
    ) -> tuple[bool, bool, bool]:
        horizontal = angle is not None and angle >= self._threshold("floor_level_torso_angle_deg", 55)
        collapsed = False
        if bbox is not None and bbox[3] > 0:
            if bbox[2] / bbox[3] > self._threshold("floor_level_bbox_aspect", 1.4):
                collapsed = True
            standing = self._standing_bbox_h()
            if standing and bbox[3] <= self._threshold("floor_level_collapse_ratio", 0.55) * standing:
                collapsed = True
        low = hip[1] >= self.floor_line_y or (
            ankle_y is not None and hip[1] >= ankle_y - self._threshold("floor_level_ankle_margin_bl", 0.5) * scale
        )
        return horizontal, collapsed, low

    def _update_floor_latch(
        self, floor_level_now: bool, confidence: float, zone: str | None, now: int, events: list[dict[str, Any]]
    ) -> None:
        latch_window_ms = self._threshold("floor_level_min_s", 2.0) * 1000
        unlatch_window_ms = self._threshold("floor_level_unlatch_s", 1.0) * 1000
        fraction = self._threshold("floor_level_latch_fraction", 0.7)
        self._floor_history.append((now, floor_level_now))
        while self._floor_history and now - self._floor_history[0][0] > latch_window_ms + 1000:
            self._floor_history.popleft()

        if not self._floor_level:
            window = [value for ts, value in self._floor_history if now - ts <= latch_window_ms]
            spans_window = self._floor_history and now - self._floor_history[0][0] >= latch_window_ms
            if spans_window and window and sum(window) / len(window) >= fraction:
                self._floor_level = True
                events.append(
                    self._payload("floor_level_posture", True, now, zone_id=zone or "", confidence=confidence)
                )
            return

        recent = [value for ts, value in self._floor_history if now - ts <= unlatch_window_ms]
        if len(recent) >= 3 and sum(1 for value in recent if not value) / len(recent) >= fraction:
            self._floor_level = False
            self._fall_suspected = False
            events.append(self._payload("floor_level_posture", False, now, zone_id=zone or ""))

    # ------------------------------------------------------------------
    # Motion

    def _update_no_motion(
        self,
        motion_score: float,
        zone: str | None,
        now: int,
        events: list[dict[str, Any]],
        evidence_allowed: bool,
        full_body_confidence: float,
    ) -> None:
        high_confidence = full_body_confidence >= self._threshold("fall_suspected_min_full_body_confidence", 0.65)
        if not evidence_allowed or not high_confidence:
            self._last_motion_ms = now
            self._motion_streak = 0
            if self._no_motion:
                self._no_motion = False
                events.append(self._payload("no_motion", False, now, zone_id=zone or ""))
            return
        confirm_frames = int(self._threshold("motion_confirm_frames", 2))
        if motion_score >= self._threshold("motion_score_threshold_bl", 0.15):
            self._motion_streak += 1
            if self._motion_streak >= confirm_frames:
                self._last_motion_ms = now
            if self._no_motion and self._motion_streak >= max(3, confirm_frames):
                self._no_motion = False
                events.append(self._payload("no_motion", False, now, zone_id=self._last_zone or ""))
            return
        self._motion_streak = 0
        if self._last_motion_ms is None:
            self._last_motion_ms = now
            return
        if not self._no_motion and now - self._last_motion_ms >= self._threshold("no_motion_after_s", 10.0) * 1000:
            self._no_motion = True
            events.append(
                self._payload("no_motion", True, now, zone_id=zone or "", confidence=full_body_confidence)
            )

    def _motion_score_bl(self, frame: FrameFeatures) -> float:
        """Median keypoint displacement in body-lengths per second, with a
        per-keypoint deadband so landmark jitter scores as zero motion."""
        score = 0.0
        if (
            frame.person_present
            and frame.keypoints
            and self._prev_keypoints
            and self._prev_keypoints_ms is not None
            and frame.timestamp_ms > self._prev_keypoints_ms
        ):
            common = frame.keypoints.keys() & self._prev_keypoints.keys()
            if common:
                deadband = self._threshold("motion_deadband", 0.005)
                displacements = sorted(
                    max(
                        0.0,
                        math.hypot(
                            frame.keypoints[name][0] - self._prev_keypoints[name][0],
                            frame.keypoints[name][1] - self._prev_keypoints[name][1],
                        )
                        - deadband,
                    )
                    for name in common
                )
                median = displacements[len(displacements) // 2]
                dt_s = (frame.timestamp_ms - self._prev_keypoints_ms) / 1000.0
                score = median / dt_s / self._body_scale()
        if frame.person_present and frame.keypoints:
            self._prev_keypoints = dict(frame.keypoints)
            self._prev_keypoints_ms = frame.timestamp_ms
        elif not frame.person_present:
            self._prev_keypoints = None
            self._prev_keypoints_ms = None
        return score

    # ------------------------------------------------------------------
    # Suppression and resets

    def _clear_fall_evidence(self, zone: str | None, now: int, events: list[dict[str, Any]]) -> None:
        if self._fall_suspected:
            events.append(self._payload("fall_suspected", False, now, zone_id=zone or ""))
        if self._floor_level:
            events.append(self._payload("floor_level_posture", False, now, zone_id=zone or ""))
        if self._no_motion:
            events.append(self._payload("no_motion", False, now, zone_id=zone or ""))
        self._fall_suspected = False
        self._rapid_drop_confirmed = False
        self._floor_level = False
        self._no_motion = False
        self._drop_pending_since = None
        self._floor_history.clear()
        self._last_motion_ms = now
        self._motion_streak = 0

    def _fall_suppressed_by_zone(self, frame: FrameFeatures, hip: tuple[float, float] | None, zone: str | None) -> bool:
        if zone in self.fall_suppressed_zone_ids:
            return True
        min_fraction = self._threshold("fall_suppression_min_fraction", 0.5)
        body_points = list(frame.keypoints.values())
        if hip:
            body_points.append(hip)
        if not body_points:
            return False
        for zone_id in self.fall_suppressed_zone_ids:
            zone_config = self.zones.get(zone_id)
            if not zone_config:
                continue
            inside = sum(1 for x, y in body_points if point_in_polygon(x, y, zone_config["polygon"]))
            if inside / len(body_points) >= min_fraction:
                return True
        return False

    def _reset_tracking_state(self) -> None:
        # Losing the pose is not evidence of recovery or of leaving bed:
        # bed occupancy stays as-is (blanket occlusion) and the fall /
        # floor-level / no-motion latches survive short dropouts so a fall
        # escalation in the state machine keeps running while blind. Only
        # frame-to-frame tracking history is dropped.
        self._hip_history.clear()
        self._floor_history.clear()
        self._torso_history.clear()
        self._smoothed = {}
        self._smoothed_bbox = None
        self._last_smooth_ms = None
        self._glitch_streak = 0
        self._prev_keypoints = None
        self._prev_keypoints_ms = None
        self._motion_streak = 0
        self._drop_pending_since = None
        self._bed_candidate_since = None
        self._bed_exit_candidate_since = None
        self._zone_candidate = None
        self._zone_candidate_since = None

    def _wipe_incident_evidence(self) -> None:
        self._fall_suspected = False
        self._rapid_drop_confirmed = False
        self._floor_level = False
        self._no_motion = False
        self._last_motion_ms = None
        self._evidence_wiped = True

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
