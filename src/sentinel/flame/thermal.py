"""Thermal observations of a field burn.

Two parts:
- `SimulatedFireField`: a deterministic cellular model of a crop-residue fire
  (wind-driven spread, a fuel-free fireguard, smouldering residue, injectable
  spot fires outside the boundary). It exists so the monitoring workflow can be
  tested; it is not a fire-behaviour model and predicts nothing.
- `ThermalObserver`: what a drone's thermal camera can see right now. It uses
  the same camera geometry as drone inspections (`drone.observation`): only
  cells inside the camera footprint are observed, positions carry an
  uncertainty, and nothing is reported when the pose does not support it.

Real thermal ingestion is not implemented: the SIYI ZT6's radiometric
interface has not been validated (`REAL_THERMAL_STATUS`). Standard library only."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from senior_safety.pose_events import point_in_polygon

from ..drone.base import geo_to_offset, offset_to_geo
from ..drone.observation import CameraModel, ground_to_image, image_to_ground, pose_from_telemetry
from .fields import distance_to_edge_m, local

# The ZT6 thermal channel. The field of view is derived from the published lens
# and core (13 mm, 640x512, 12 µm pitch assumed); the measuring ranges come from
# distributor listings. Both need checking against SIYI's manual on the bench.
THERMAL_CAMERAS: dict[str, dict[str, Any]] = {
    "SIYI ZT6": {"hfov_deg": 32.9, "vfov_deg": 26.6, "min_c": -20.0, "max_c": 550.0, "accuracy": "±2 °C (-20..150 °C), ±5 °C (50..550 °C)",
                 "basis": "estimate: field of view derived from a 13 mm lens on a 640x512 core (12 µm pitch assumed); ranges from distributor listings"},
}
DEFAULT_THERMAL = "SIYI ZT6"
REAL_THERMAL_STATUS = ("Thermal measurements from a real aircraft are not read yet: the ZT6's radiometric data interface has not been "
                       "validated, so only the simulator produces thermal observations.")
MIN_HEIGHT_M = 5.0


@dataclass
class Hotspot:
    """One connected patch of heat seen in a single look."""

    local: list[float]  # east, north metres from the field origin
    lat: float
    lon: float
    max_c: float
    saturated: bool
    area_m2: float
    uncertainty_m: float | None
    cells: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"lat": round(self.lat, 7), "lon": round(self.lon, 7), "max_c": round(self.max_c), "saturated": self.saturated,
                "area_m2": round(self.area_m2), "uncertainty_m": self.uncertainty_m}


class SimulatedFireField:
    """Grid over the field plus a margin. Cell states: 0 unburnt, 1 flaming,
    2 smouldering, 3 out. Times are simulation seconds supplied by the caller,
    so tests are deterministic."""

    FLAMING_S = 40.0
    FLAMING_C = (650.0, 850.0)
    SMOULDER_START_C = 350.0
    SMOULDER_TAU_S = 240.0
    HEAVY_RESIDUE_SHARE = 0.04  # straw piles and windrows that smoulder much longer
    HEAVY_TAU_S = 1500.0
    OUTSIDE_FUEL = 0.5  # ditch grass and verges

    def __init__(self, boundary: list[list[float]], *, cell_m: float = 5.0, margin_m: float = 120.0, fireguard_m: float = 0.0,
                 wind_from_deg: float = 270.0, wind_mps: float = 3.0, ambient_c: float = 15.0, seed: int = 7) -> None:
        self.origin = (boundary[0][0], boundary[0][1])
        self.polygon = local(boundary, self.origin)
        self.cell_m = cell_m
        self.fireguard_m = fireguard_m
        self.wind_from_deg = wind_from_deg
        self.wind_mps = wind_mps
        self.ambient_c = ambient_c
        self.random = random.Random(seed)
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        self.x0, self.y0 = min(xs) - margin_m, min(ys) - margin_m
        self.cols = int(math.ceil((max(xs) - min(xs) + 2 * margin_m) / cell_m))
        self.rows = int(math.ceil((max(ys) - min(ys) + 2 * margin_m) / cell_m))
        count = self.cols * self.rows
        self.inside = [False] * count
        self.fuel = [0.0] * count
        self.heavy = [False] * count
        self.state = [0] * count
        self.ignited_s = [0.0] * count
        self.peak_c = [0.0] * count
        self.signed_m = [0.0] * count
        for index in range(count):
            x, y = self.center(index)
            inside = point_in_polygon(x, y, self.polygon)
            edge = distance_to_edge_m([x, y], self.polygon)
            self.inside[index] = inside
            self.signed_m[index] = -edge if inside else edge
            if inside:
                self.fuel[index] = 0.0 if edge < fireguard_m else 1.0
                self.heavy[index] = self.random.random() < self.HEAVY_RESIDUE_SHARE
            else:
                self.fuel[index] = self.OUTSIDE_FUEL
        self.burning: set[int] = set()
        self.smouldering: set[int] = set()
        self.time_s = 0.0

    # ---- geometry -------------------------------------------------------------------

    def center(self, index: int) -> tuple[float, float]:
        row, col = divmod(index, self.cols)
        return self.x0 + (col + 0.5) * self.cell_m, self.y0 + (row + 0.5) * self.cell_m

    def index_at(self, x: float, y: float) -> int | None:
        col = int((x - self.x0) // self.cell_m)
        row = int((y - self.y0) // self.cell_m)
        if 0 <= col < self.cols and 0 <= row < self.rows:
            return row * self.cols + col
        return None

    def geo(self, x: float, y: float) -> tuple[float, float]:
        return offset_to_geo(self.origin, x, y)

    def to_local(self, lat: float, lon: float) -> tuple[float, float]:
        return geo_to_offset(self.origin, lat, lon)

    def neighbours(self, index: int):
        row, col = divmod(index, self.cols)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr or dc:
                    r, c = row + dr, col + dc
                    if 0 <= r < self.rows and 0 <= c < self.cols:
                        yield r * self.cols + c, dc, dr

    # ---- fire -----------------------------------------------------------------------

    def ignite_index(self, index: int) -> bool:
        if self.state[index] != 0 or self.fuel[index] <= 0:
            return False
        self.state[index] = 1
        self.ignited_s[index] = self.time_s
        self.peak_c[index] = self.random.uniform(*self.FLAMING_C)
        self.burning.add(index)
        return True

    def ignite_point(self, lat: float, lon: float, radius_m: float = 6.0) -> int:
        x, y = self.to_local(lat, lon)
        lit = 0
        steps = int(radius_m // self.cell_m) + 1
        for dx in range(-steps, steps + 1):
            for dy in range(-steps, steps + 1):
                if math.hypot(dx, dy) * self.cell_m <= radius_m:
                    index = self.index_at(x + dx * self.cell_m, y + dy * self.cell_m)
                    if index is not None:
                        lit += self.ignite_index(index)
        return lit

    def ignite_edge(self, side_deg: float, depth_m: float = 8.0) -> int:
        """Ignite fuel just inside the boundary on one side (bearing from the
        centre, 0 = north): how a crew lights a backing fire along an edge."""
        cx = sum(p[0] for p in self.polygon) / len(self.polygon)
        cy = sum(p[1] for p in self.polygon) / len(self.polygon)
        lit = 0
        for index in range(len(self.state)):
            if not self.inside[index] or self.fuel[index] <= 0:
                continue
            x, y = self.center(index)
            bearing = math.degrees(math.atan2(x - cx, y - cy)) % 360
            if abs((bearing - side_deg + 180) % 360 - 180) <= 45 and -self.signed_m[index] <= self.fireguard_m + depth_m:
                lit += self.ignite_index(index)
        return lit

    def step(self, dt_s: float) -> None:
        self.time_s += dt_s
        wind_to = math.radians((self.wind_from_deg + 180) % 360)
        head = 0.1 + 0.15 * self.wind_mps
        back = 0.03
        ignite: list[int] = []
        for index in list(self.burning):
            if self.time_s - self.ignited_s[index] >= self.FLAMING_S:
                self.burning.discard(index)
                self.state[index] = 2
                self.smouldering.add(index)
                continue
            for other, dc, dr in self.neighbours(index):
                if self.state[other] != 0 or self.fuel[other] <= 0:
                    continue
                direction = math.atan2(dc, dr)  # east, north components of the step
                alignment = math.cos(direction - wind_to)
                speed = back + (head - back) * ((1 + alignment) / 2) ** 3
                distance = self.cell_m * math.hypot(dc, dr)
                chance = 1 - math.exp(-speed * self.fuel[other] * dt_s / distance)
                if self.random.random() < chance:
                    ignite.append(other)
        for index in ignite:
            self.ignite_index(index)
        for index in list(self.smouldering):
            if self.temperature(index) < self.ambient_c + 5:
                self.smouldering.discard(index)
                self.state[index] = 3

    def temperature(self, index: int) -> float:
        state = self.state[index]
        if state == 0 or state == 3:
            return self.ambient_c
        elapsed = self.time_s - self.ignited_s[index]
        if state == 1:
            return self.peak_c[index]
        tau = self.HEAVY_TAU_S if self.heavy[index] else self.SMOULDER_TAU_S
        cooling = elapsed - self.FLAMING_S
        return self.ambient_c + (self.SMOULDER_START_C - self.ambient_c) * math.exp(-cooling / tau)

    def stats(self) -> dict[str, Any]:
        cell_area = self.cell_m ** 2
        flaming_inside = sum(1 for i in self.burning if self.inside[i])
        flaming_outside = len(self.burning) - flaming_inside
        burnt = sum(1 for i, s in enumerate(self.state) if s in (2, 3) and self.inside[i])
        fuel_cells = sum(1 for i in range(len(self.state)) if self.inside[i] and self.fuel[i] > 0)
        return {"time_s": round(self.time_s), "flaming_m2_inside": flaming_inside * cell_area, "flaming_m2_outside": flaming_outside * cell_area,
                "burnt_share": round(burnt / fuel_cells, 3) if fuel_cells else 0.0, "smouldering_cells": len(self.smouldering)}


@dataclass
class Look:
    """One thermal look: what was in view and the hotspots in it."""

    ok: bool
    reason: str = ""
    observed: list[int] = field(default_factory=list)
    hotspots: list[Hotspot] = field(default_factory=list)
    footprint_m2: float = 0.0


class ThermalObserver:
    def __init__(self, camera: dict[str, Any] | None = None, *, threshold_c: float = 150.0) -> None:
        spec = camera or THERMAL_CAMERAS[DEFAULT_THERMAL]
        self.spec = spec
        self.camera = CameraModel(hfov_deg=spec["hfov_deg"], vfov_deg=spec["vfov_deg"], default_pitch_deg=-90.0)
        self.threshold_c = threshold_c

    def look(self, fire: SimulatedFireField, telemetry: Any, now_ms: int) -> Look:
        if not telemetry.connected or not telemetry.link_ok:
            return Look(False, "no telemetry from the drone")
        if not telemetry.in_air:
            return Look(False, "drone on the ground")
        pose = pose_from_telemetry(telemetry, self.camera, now_ms)
        if pose is None:
            return Look(False, "no drone position")
        if pose.height_m < MIN_HEIGHT_M:
            return Look(False, f"drone below {MIN_HEIGHT_M:g} m: no useful thermal view")
        corners = [image_to_ground(pose, self.camera, u, v) for u, v in ((0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5))]
        points = [fire.to_local(c.lat, c.lon) for c in corners if c.available]
        if not points:
            return Look(False, "the camera does not look at the ground")
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        observed: dict[int, None] = {}
        hot: dict[int, float] = {}
        y = min(ys)
        while y <= max(ys) + fire.cell_m:
            x = min(xs)
            while x <= max(xs) + fire.cell_m:
                index = fire.index_at(x, y)
                if index is not None:
                    cx, cy = fire.center(index)
                    lat, lon = fire.geo(cx, cy)
                    uv = ground_to_image(pose, self.camera, lat, lon)
                    if uv and 0 <= uv[0] <= 1 and 0 <= uv[1] <= 1 and index not in observed:
                        observed[index] = None
                        temperature = fire.temperature(index)
                        if temperature >= self.threshold_c:
                            hot[index] = temperature
                x += fire.cell_m
            y += fire.cell_m
        hotspots = [self._hotspot(fire, cluster, hot, pose) for cluster in _clusters(fire, set(hot))]
        return Look(True, observed=list(observed), hotspots=hotspots, footprint_m2=len(observed) * fire.cell_m ** 2)

    def _hotspot(self, fire: SimulatedFireField, cluster: list[int], hot: dict[int, float], pose: Any) -> Hotspot:
        centers = [fire.center(i) for i in cluster]
        x = sum(c[0] for c in centers) / len(centers)
        y = sum(c[1] for c in centers) / len(centers)
        lat, lon = fire.geo(x, y)
        peak = max(hot[i] for i in cluster)
        uncertainty = None
        uv = ground_to_image(pose, self.camera, lat, lon)
        if uv:
            estimate = image_to_ground(pose, self.camera, *uv)
            uncertainty = estimate.uncertainty_m
        return Hotspot(local=[x, y], lat=lat, lon=lon, max_c=min(peak, self.spec["max_c"]), saturated=peak >= self.spec["max_c"],
                       area_m2=len(cluster) * fire.cell_m ** 2, uncertainty_m=uncertainty, cells=cluster)


def _clusters(fire: SimulatedFireField, cells: set[int]) -> list[list[int]]:
    clusters, seen = [], set()
    for start in cells:
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            index = stack.pop()
            group.append(index)
            for other, _, _ in fire.neighbours(index):
                if other in cells and other not in seen:
                    seen.add(other)
                    stack.append(other)
        clusters.append(group)
    return clusters


__all__ = ["DEFAULT_THERMAL", "Hotspot", "Look", "REAL_THERMAL_STATUS", "SimulatedFireField", "THERMAL_CAMERAS", "ThermalObserver"]
