from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .models import Track


@dataclass
class ZonePresence:
    since_ms: int
    left_ms: int | None = None


@dataclass
class SiteTrack:
    """One object as seen across the property. Camera tracks are joined into a
    site track only when timing, topology, and (if available) appearance agree
    and no competing candidate is comparably likely. Otherwise a new site track
    is started: an unlinked duplicate is safer than a wrong identity merge."""

    site_track_id: str
    label: str
    first_seen_ms: int
    last_seen_ms: int
    last_camera: str
    members: list[str] = field(default_factory=list)
    active_members: set[str] = field(default_factory=set)
    camera_last_seen: dict[str, int] = field(default_factory=dict)
    last_anchor: dict[str, tuple[float, float]] = field(default_factory=dict)
    last_zones: dict[str, list[str]] = field(default_factory=dict)
    link_confidence: float = 1.0
    links: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_with: list[str] = field(default_factory=list)
    appearance: list[float] | None = None
    arrived: bool = False
    zones: dict[str, ZonePresence] = field(default_factory=dict)
    path: list[tuple[int, str, str]] = field(default_factory=list)  # (ms, camera, zone)
    fired: set[str] = field(default_factory=set)
    activity_logged: bool = False

    def cameras_since(self, since_ms: int) -> list[str]:
        return sorted(camera for camera, seen in self.camera_last_seen.items() if seen >= since_ms)

    def summary(self) -> dict[str, Any]:
        return {
            "site_track_id": self.site_track_id,
            "label": self.label,
            "first_seen_ms": self.first_seen_ms,
            "last_seen_ms": self.last_seen_ms,
            "cameras": sorted(self.camera_last_seen),
            "members": list(self.members),
            "active": bool(self.active_members),
            "link_confidence": round(self.link_confidence, 3),
            "ambiguous_with": list(self.ambiguous_with),
        }


@dataclass
class CorrelationConfig:
    reacquire_s: float = 4.0
    reacquire_distance: float = 0.15
    min_link_confidence: float = 0.5
    ambiguity_margin: float = 0.15
    retain_s: float = 600.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "CorrelationConfig":
        data = data or {}
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


def appearance_similarity(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else None


class CrossCameraCorrelator:
    def __init__(self, links: list[dict[str, Any]] | None = None, config: CorrelationConfig | None = None) -> None:
        self.config = config or CorrelationConfig()
        self.links: list[dict[str, Any]] = []
        for link in links or []:
            self.links.append(link)
            if link.get("bidirectional", True):
                self.links.append(
                    link
                    | {
                        "from": link["to"],
                        "to": link["from"],
                        "from_zones": link.get("to_zones", []),
                        "to_zones": link.get("from_zones", []),
                    }
                )
        self.site_tracks: dict[str, SiteTrack] = {}
        self.track_to_site: dict[str, str] = {}
        self._next_id = 1
        self.ambiguous_count = 0

    def assign(self, track: Track, timestamp_ms: int, zones_now: list[str]) -> SiteTrack:
        site_id = self.track_to_site.get(track.track_id)
        if site_id is None or site_id not in self.site_tracks:
            site = self._link_or_create(track, timestamp_ms, zones_now)
        else:
            site = self.site_tracks[site_id]
        site.last_seen_ms = timestamp_ms
        site.last_camera = track.camera_id
        site.camera_last_seen[track.camera_id] = timestamp_ms
        site.last_anchor[track.camera_id] = track.anchor
        if zones_now:
            site.last_zones[track.camera_id] = list(zones_now)
        site.active_members.add(track.track_id)
        if track.appearance:
            site.appearance = track.appearance
        return site

    def end(self, track: Track, timestamp_ms: int) -> SiteTrack | None:
        site_id = self.track_to_site.get(track.track_id)
        site = self.site_tracks.get(site_id or "")
        if site:
            site.active_members.discard(track.track_id)
        return site

    def expire(self, now_ms: int) -> list[SiteTrack]:
        retain_ms = self.config.retain_s * 1000
        expired = [
            site
            for site in self.site_tracks.values()
            if not site.active_members and now_ms - site.last_seen_ms > retain_ms
        ]
        for site in expired:
            del self.site_tracks[site.site_track_id]
            for member in site.members:
                self.track_to_site.pop(member, None)
        return expired

    def active(self) -> list[SiteTrack]:
        return [site for site in self.site_tracks.values() if site.active_members]

    def _link_or_create(self, track: Track, timestamp_ms: int, zones_now: list[str]) -> SiteTrack:
        candidates = sorted(self._candidates(track, timestamp_ms, zones_now), key=lambda item: item[0], reverse=True)
        chosen: tuple[float, SiteTrack, str] | None = None
        ambiguous_with: list[str] = []
        if candidates and candidates[0][0] >= self.config.min_link_confidence:
            if len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= self.config.ambiguity_margin:
                chosen = candidates[0]
            else:
                ambiguous_with = [site.site_track_id for _, site, _ in candidates[:4]]
                self.ambiguous_count += 1

        if chosen:
            confidence, site, kind = chosen
            site.links.append(
                {
                    "to_track": track.track_id,
                    "from_camera": site.last_camera,
                    "to_camera": track.camera_id,
                    "kind": kind,
                    "confidence": round(confidence, 3),
                    "timestamp_ms": timestamp_ms,
                }
            )
            if kind == "camera_link":
                site.link_confidence = min(site.link_confidence, confidence)
                site.arrived = True
        else:
            site = SiteTrack(
                site_track_id=f"st_{self._next_id}",
                label=track.label,
                first_seen_ms=timestamp_ms,
                last_seen_ms=timestamp_ms,
                last_camera=track.camera_id,
                ambiguous_with=ambiguous_with,
            )
            self._next_id += 1
            self.site_tracks[site.site_track_id] = site
        site.members.append(track.track_id)
        self.track_to_site[track.track_id] = site.site_track_id
        return site

    def _candidates(self, track: Track, timestamp_ms: int, zones_now: list[str]) -> list[tuple[float, SiteTrack, str]]:
        results: list[tuple[float, SiteTrack, str]] = []
        for site in self.site_tracks.values():
            if site.label != track.label:
                continue
            if any(member.startswith(f"{track.camera_id}:") for member in site.active_members):
                continue  # already visible on this camera: a different object

            best: tuple[float, str] | None = None
            last_on_camera = site.camera_last_seen.get(track.camera_id)
            if not site.active_members and last_on_camera is not None:
                gap_s = (timestamp_ms - last_on_camera) / 1000
                anchor = site.last_anchor.get(track.camera_id)
                if 0 <= gap_s <= self.config.reacquire_s and anchor is not None:
                    distance = math.hypot(anchor[0] - track.anchor[0], anchor[1] - track.anchor[1])
                    if distance <= self.config.reacquire_distance:
                        timing = 1 - gap_s / self.config.reacquire_s
                        nearness = 1 - distance / self.config.reacquire_distance
                        best = (0.6 + 0.2 * timing + 0.2 * nearness, "reacquired")

            for link in self.links:
                if link["to"] != track.camera_id or link["from"] not in site.camera_last_seen:
                    continue
                overlap = bool(link.get("overlap", False))
                active_elsewhere = any(not m.startswith(f"{link['from']}:") for m in site.active_members)
                active_on_from = any(m.startswith(f"{link['from']}:") for m in site.active_members)
                if active_elsewhere or (active_on_from and not overlap):
                    continue
                gap_s = (timestamp_ms - site.camera_last_seen[link["from"]]) / 1000
                low = float(link.get("min_transit_s", 0))
                high = float(link["max_transit_s"])
                if not (low <= gap_s <= high or (overlap and active_on_from)):
                    continue
                from_zones = link.get("from_zones") or []
                if from_zones and not set(site.last_zones.get(link["from"], [])) & set(from_zones):
                    continue
                to_zones = link.get("to_zones") or []
                if to_zones and not set(zones_now) & set(to_zones):
                    continue
                middle = (low + high) / 2
                timing = 1.0 if overlap and active_on_from else 1 - min(1.0, abs(gap_s - middle) / max(high - low, 1e-6))
                similarity = appearance_similarity(site.appearance, track.appearance)
                appearance = 0.5 if similarity is None else max(0.0, similarity)
                score = 0.4 + 0.25 * timing + 0.35 * appearance
                if best is None or score > best[0]:
                    best = (score, "camera_link")
            if best:
                results.append((best[0], site, best[1]))
        return results
