"""Relative movement of one tracked object from its recent box history (stdlib only).

Everything here is measured in the image. Speeds are relative: "body heights per
second" (box heights travelled per second) and the box's growth rate. A growing
box usually means the object is getting closer to the camera, but posture
changes, partial occlusion and the frame edge also change box size, so growth is
ignored when the box touches the frame edge and a noisy fit is reported as
`unclear`, never as a direction."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

# (timestamp_ms, anchor_x, anchor_y, box_w, box_h, confidence, touches_edge)
Sample = tuple[int, float, float, float, float, float, bool]

MOVEMENTS = ("stationary", "approaching", "retreating", "lateral", "unclear")


@dataclass
class MovementSettings:
    window_s: float = 2.5
    min_samples: int = 4
    min_span_s: float = 1.0
    moving_speed: float = 0.15  # body heights per second below which an object counts as stationary
    approach_rate: float = 0.06  # box growth per second (6 %/s) that counts as approaching the camera
    max_scale_noise: float = 0.12  # residual of log box height above which the scale trend is not trusted
    turn_angle_deg: float = 100.0
    turn_min_speed: float = 0.3
    jump_body_heights: float = 2.5  # a jump this large between samples is a tracking glitch, not movement

    @classmethod
    def from_dict(cls, data: dict | None) -> "MovementSettings":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class Trend:
    slope: float
    intercept: float
    residual: float  # RMS residual around the fitted line


def trend(times_s: Sequence[float], values: Sequence[float]) -> Trend:
    n = len(times_s)
    mean_t = sum(times_s) / n
    mean_v = sum(values) / n
    var_t = sum((t - mean_t) ** 2 for t in times_s)
    slope = 0.0 if var_t <= 0 else sum((t - mean_t) * (v - mean_v) for t, v in zip(times_s, values)) / var_t
    intercept = mean_v - slope * mean_t
    residual = math.sqrt(sum((v - (intercept + slope * t)) ** 2 for t, v in zip(times_s, values)) / n)
    return Trend(slope, intercept, residual)


@dataclass
class MovementEstimate:
    movement: str = "unclear"
    reasons: list[str] = field(default_factory=list)
    relative_speed: float = 0.0  # body heights per second, in the image
    growth_rate: float = 0.0  # d ln(box height)/dt; > 0 means the box is growing
    image_heading: str = ""  # left / right / up / down in the picture, or "" when stationary
    velocity: tuple[float, float] = (0.0, 0.0)  # normalised image units per second
    body_height: float = 0.0
    turned: bool = False
    turn_angle_deg: float = 0.0
    sped_up: bool = False
    scale_trusted: bool = False
    samples: int = 0
    span_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["relative_speed"] = round(self.relative_speed, 3)
        data["growth_rate"] = round(self.growth_rate, 3)
        data["velocity"] = [round(v, 4) for v in self.velocity]
        data["body_height"] = round(self.body_height, 4)
        data["turn_angle_deg"] = round(self.turn_angle_deg, 1)
        return data


def recent(samples: Sequence[Sample], now_ms: int, window_s: float) -> list[Sample]:
    cutoff = now_ms - window_s * 1000
    return [sample for sample in samples if sample[0] >= cutoff]


def estimate(samples: Sequence[Sample], now_ms: int, settings: MovementSettings | None = None) -> MovementEstimate:
    settings = settings or MovementSettings()
    window = recent(samples, now_ms, settings.window_s)
    result = MovementEstimate(samples=len(window))
    if len(window) < settings.min_samples:
        result.reasons.append("too_few_observations")
        return result
    t0 = window[0][0]
    times = [(s[0] - t0) / 1000 for s in window]
    result.span_s = times[-1]
    if result.span_s < settings.min_span_s:
        result.reasons.append("observed_too_briefly")
        return result
    body = sorted(s[4] for s in window)[len(window) // 2] or 1e-6
    result.body_height = body
    tx = trend(times, [s[1] for s in window])
    ty = trend(times, [s[2] for s in window])
    result.velocity = (tx.slope, ty.slope)
    result.relative_speed = math.hypot(tx.slope, ty.slope) / body

    edge = sum(1 for s in window if s[6]) > len(window) // 3
    tscale = trend(times, [math.log(max(s[4], 1e-6)) for s in window])
    result.scale_trusted = not edge and tscale.residual <= settings.max_scale_noise
    result.growth_rate = tscale.slope if result.scale_trusted else 0.0
    if edge:
        result.reasons.append("box_at_frame_edge")
    elif not result.scale_trusted:
        result.reasons.append("box_size_noisy")

    moving = result.relative_speed >= settings.moving_speed or abs(result.growth_rate) >= settings.approach_rate
    if moving and result.relative_speed >= settings.moving_speed:
        vx, vy = result.velocity
        result.image_heading = ("right" if vx > 0 else "left") if abs(vx) >= abs(vy) else ("down" if vy > 0 else "up")
    position_noise = math.hypot(tx.residual, ty.residual) / body
    if position_noise > 0.6 and not result.scale_trusted:
        result.movement = "unclear"
        result.reasons.append("position_noisy")
    elif not moving:
        result.movement = "stationary"
    elif result.growth_rate >= settings.approach_rate:
        result.movement = "approaching"
        result.reasons.append("box_growing")
    elif result.growth_rate <= -settings.approach_rate:
        result.movement = "retreating"
        result.reasons.append("box_shrinking")
    elif result.relative_speed >= settings.moving_speed:
        result.movement = "lateral"
    else:
        result.movement = "unclear"

    _turns(samples, now_ms, settings, result, body)
    return result


def _turns(samples: Sequence[Sample], now_ms: int, settings: MovementSettings, result: MovementEstimate, body: float) -> None:
    """Compare the heading of the latest window with the one before it."""
    span = settings.window_s * 1000
    older = [s for s in samples if now_ms - 2 * span <= s[0] < now_ms - span]
    newer = recent(samples, now_ms, settings.window_s)
    if len(older) < settings.min_samples or len(newer) < settings.min_samples:
        return

    def velocity(part: Sequence[Sample]) -> tuple[float, float]:
        times = [(s[0] - part[0][0]) / 1000 for s in part]
        return trend(times, [s[1] for s in part]).slope, trend(times, [s[2] for s in part]).slope

    (ax, ay), (bx, by) = velocity(older), velocity(newer)
    speed_a, speed_b = math.hypot(ax, ay) / body, math.hypot(bx, by) / body
    if speed_a >= settings.turn_min_speed and speed_b >= settings.turn_min_speed:
        cos = (ax * bx + ay * by) / (math.hypot(ax, ay) * math.hypot(bx, by))
        result.turn_angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        result.turned = result.turn_angle_deg >= settings.turn_angle_deg
        if result.turned:
            result.reasons.append("sudden_direction_change")
    if speed_b >= 0.5 and speed_b >= 2 * max(speed_a, 0.05):
        result.sped_up = True
        result.reasons.append("speed_increase")


def distance_trend(series: Sequence[tuple[int, float]], now_ms: int, window_s: float) -> tuple[float, float] | None:
    """(slope per second, latest fitted value) of a distance series over the window."""
    cutoff = now_ms - window_s * 1000
    points = [(t, d) for t, d in series if t >= cutoff]
    if len(points) < 4 or (points[-1][0] - points[0][0]) < 1000:
        return None
    t0 = points[0][0]
    times = [(t - t0) / 1000 for t, _ in points]
    fit = trend(times, [d for _, d in points])
    return fit.slope, fit.intercept + fit.slope * times[-1]
