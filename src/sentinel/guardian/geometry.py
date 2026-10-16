"""Image and ground-plane geometry for movement intelligence (stdlib only).

Image coordinates are normalised to the frame (0..1). Ground coordinates exist
only when a camera has a ground-plane calibration (image points paired with
measured ground points in metres); without one, Sentinel never reports metres,
real speeds or times to reach a zone."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

Point = tuple[float, float]


def point_in_polygon(x: float, y: float, polygon: Sequence[Sequence[float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def distance_to_polygon(point: Point, polygon: Sequence[Sequence[float]]) -> float:
    """0 inside the polygon, otherwise distance to its nearest edge."""
    if point_in_polygon(point[0], point[1], polygon):
        return 0.0
    best = math.inf
    for index in range(len(polygon)):
        a, b = polygon[index], polygon[(index + 1) % len(polygon)]
        best = min(best, _segment_distance(point, (a[0], a[1]), (b[0], b[1])))
    return best


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; raises on a singular system."""
    size = len(vector)
    rows = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("calibration points are degenerate (three or more in a line?)")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(size):
            if r != col:
                factor = rows[r][col] / rows[col][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[col])]
    return [rows[i][size] / rows[i][i] for i in range(size)]


@dataclass
class GroundCalibration:
    """Planar homography from normalised image points to ground metres, fitted by
    least squares. `rms_error_m` is measured on the calibration points themselves
    (0 with exactly four points, which is why four points are reported as
    `unverified`). It assumes flat ground and that the ground point of a box is
    the animal's feet, both of which can be wrong."""

    h: list[float]
    rms_error_m: float
    points: int
    # Leave-one-out check: each point predicted by a fit on the others. Unlike the
    # fit residual, it catches a mis-measured mark the least-squares fit absorbed.
    check_error_m: float | None = None

    @classmethod
    def fit(cls, image_points: Sequence[Sequence[float]], ground_points: Sequence[Sequence[float]], *, check: bool = True) -> "GroundCalibration":
        if len(image_points) != len(ground_points) or len(image_points) < 4:
            raise ValueError("calibration needs at least four matching image and ground points")
        normal = [[0.0] * 8 for _ in range(8)]
        rhs = [0.0] * 8
        for (x, y), (gx, gy) in zip(image_points, ground_points):
            for row, target in (([x, y, 1, 0, 0, 0, -x * gx, -y * gx], gx), ([0, 0, 0, x, y, 1, -x * gy, -y * gy], gy)):
                for i in range(8):
                    rhs[i] += row[i] * target
                    for j in range(8):
                        normal[i][j] += row[i] * row[j]
        calibration = cls(_solve(normal, rhs) + [1.0], 0.0, len(image_points))
        errors = [math.dist(calibration.to_ground((x, y)), (gx, gy)) for (x, y), (gx, gy) in zip(image_points, ground_points)]
        calibration.rms_error_m = math.sqrt(sum(e * e for e in errors) / len(errors))
        if check and len(image_points) > 4:
            held_out = []
            for index in range(len(image_points)):
                rest = [i for i in range(len(image_points)) if i != index]
                try:
                    partial = cls.fit([image_points[i] for i in rest], [ground_points[i] for i in rest], check=False)
                    held_out.append(math.dist(partial.to_ground(tuple(image_points[index])), tuple(ground_points[index])))
                except ValueError:
                    held_out.append(math.inf)
            calibration.check_error_m = math.sqrt(sum(e * e for e in held_out) / len(held_out))
        return calibration

    @classmethod
    def from_config(cls, data: dict | None) -> "GroundCalibration | None":
        if not data:
            return None
        return cls.fit(data["image_points"], data["ground_points_m"])

    def to_ground(self, point: Point) -> Point:
        h = self.h
        x, y = point
        w = h[6] * x + h[7] * y + h[8]
        if abs(w) < 1e-9:
            raise ValueError("point is on the horizon line")
        return ((h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w)

    @property
    def verified(self) -> bool:
        return self.points > 4 and self.rms_error_m <= 0.5 and self.check_error_m is not None and self.check_error_m <= 0.5

    @property
    def status(self) -> str:
        if self.verified:
            return "verified"
        if self.check_error_m is None:
            return "unverified (four points, no check)"
        error = "a degenerate point set" if math.isinf(self.check_error_m) else f"{self.check_error_m:.1f} m"
        return f"unverified (leave-one-out check error {error}; re-measure the marks)"
