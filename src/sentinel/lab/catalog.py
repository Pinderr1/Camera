"""Test Lab clip catalog (stdlib).

Clips come from the registries Sentinel already keeps, so licences and labels
have one source of truth:

  * config/bench/manifest.json   MOT16 / CAVIAR with per-frame boxes (evaluation only)
  * config/guardian/videos.json  trail-camera wildlife clips with presence ranges
  * config/lab/videos.json       commercially usable test clips (docs/datasets.md)
  * <storage>/lab/videos/        the owner's own footage (private, never leaves the machine)

Each clip carries its split. Data used to tune Sentinel is `validation`; the
lab clips nobody tuned on are `test`; nothing is `train` until the owner builds a
training set (sentinel.training). Accuracy shown in the lab is only reported
against labels and always names the clip it came from."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mkv", ".mov", ".ogv", ".m4v"}
BENCH_SITE = "config/bench/site.json"


@dataclass
class LabClip:
    id: str
    title: str
    source: str  # bench | guardian | lab | private
    category: str
    subjects: list[str]
    light: str
    licence: str
    commercial_training: bool  # may the clip be used to train a model that is sold
    split: str  # validation | test | private
    labels: str  # boxes | presence | none
    conditions: list[str] = field(default_factory=list)
    notes: str = ""
    attribution: str = ""
    path: str = ""  # server side only (never returned by the API)
    label_spec: dict[str, Any] | None = None  # bench label file spec
    present_s: list[list[float]] = field(default_factory=list)
    absent_s: list[list[float]] = field(default_factory=list)
    expected_species: dict[str, Any] | None = None
    bench_camera: str = ""  # camera id in config/bench/site.json (zones, rules)

    def public(self) -> dict[str, Any]:
        view = asdict(self)
        for key in ("path", "label_spec"):
            view.pop(key)
        view["available"] = Path(self.path).exists()
        return view


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _commercial(licence: str) -> bool:
    text = licence.lower()
    tokens = set(re.split(r"[^a-z0-9]+", text))
    if tokens & {"nc", "nd"} or any(term in text for term in ("non-commercial", "noncommercial", "research", "evaluation only")):
        return False
    return any(term in text for term in ("public domain", "cc0", "cc by", "cdla-permissive", "apache", "mit"))


def bench_clips(root: Path = ROOT) -> list[LabClip]:
    manifest = _load(root / "config/bench/manifest.json")
    clips = []
    for spec in manifest.get("clips", []):
        if spec["id"].endswith("_sub"):
            continue  # same footage as the main-stream clip
        labels = spec.get("labels")
        source_text = spec.get("label_source") or ""
        caviar = spec["id"].startswith("caviar")
        licence = "CC BY-NC-SA 3.0 (MOTChallenge)" if spec["id"].startswith("mot16") else "CAVIAR research use" if caviar else source_text or "unknown"
        clips.append(LabClip(
            id=spec["id"], title=spec["id"].replace("_", " ").upper() if spec["id"].startswith("mot") else spec["id"].replace("_", " ").title(),
            source="bench", category="security_people", subjects=["person"],
            light="night" if "night" in spec.get("conditions", []) else "day",
            licence=licence, commercial_training=False, split="validation", labels="boxes" if labels else "none",
            conditions=list(spec.get("conditions", [])), notes=source_text, attribution=source_text,
            path=str(root / spec["video"]), label_spec=_absolute_labels(labels, root), bench_camera=spec["camera_id"],
        ))
    return clips


def _absolute_labels(labels: dict[str, Any] | None, root: Path) -> dict[str, Any] | None:
    if not labels:
        return None
    out = dict(labels)
    for key in ("path", "dir"):
        if key in out and not Path(out[key]).is_absolute():
            out[key] = str(root / out[key])
    return out


def _registry_clips(registry: Path, folder: Path, source: str, default_split: str, default_category: str) -> list[LabClip]:
    data = _load(registry)
    clips = []
    for item in data.get("clips", []):
        path = Path(item["path"]) if item.get("path") else _find_video(folder, item["id"])
        subjects = item.get("subjects") or (["animal"] if item.get("animal") else ["none"])
        labelled = bool(item.get("present_s") or item.get("absent_s"))
        clips.append(LabClip(
            id=item["id"], title=item.get("name") or item.get("animal") or item.get("title") or item["id"], source=source,
            category=item.get("category") or (default_category if item.get("animal") else "empty_scene"),
            subjects=subjects, light=item.get("light", "day"), licence=item.get("licence", "unknown"),
            commercial_training=bool(item.get("commercial_training", _commercial(item.get("licence", "")))), split=item.get("split", default_split),
            labels="presence" if labelled else "none", conditions=list(item.get("conditions", [])),
            notes=" ".join(filter(None, [item.get("notes", ""), item.get("licence_note", "")])), attribution=f"{item.get('title', '')} — {item.get('author', '')} ({item.get('licence', '')})",
            path=str(path), present_s=item.get("present_s", []), absent_s=item.get("absent_s", []),
            expected_species=item.get("expected"),
        ))
    return clips


def _find_video(folder: Path, clip_id: str) -> Path:
    for extension in (".webm", ".mp4", ".ogv", ".mkv", ".avi", ".mov"):
        candidate = folder / f"{clip_id}{extension}"
        if candidate.exists():
            return candidate
    return folder / f"{clip_id}.webm"


def private_clips(storage_dir: Path) -> list[LabClip]:
    """The owner's own recordings, dropped into <storage>/lab/videos/. Never used
    for training unless the owner turns on personalization (sentinel.training)."""
    folder = storage_dir / "lab" / "videos"
    clips = []
    if not folder.is_dir():
        return clips
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        labels = _load(path.with_suffix(".labels.json"))  # optional: {"present_s": [...], "absent_s": [...]}
        clip_id = "own_" + re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_")
        clips.append(LabClip(
            id=clip_id, title=path.stem, source="private", category="own_footage", subjects=labels.get("subjects", []),
            light=labels.get("light", "unknown"), licence="Owner's footage (private)", commercial_training=False,
            split="private", labels="presence" if labels.get("present_s") or labels.get("absent_s") else "none",
            notes="Your own recording. Stays on this computer.", path=str(path),
            present_s=labels.get("present_s", []), absent_s=labels.get("absent_s", []),
        ))
    return clips


def all_clips(storage_dir: Path, root: Path = ROOT) -> list[LabClip]:
    return (
        _registry_clips(root / "config/lab/videos.json", root / "data/lab/videos", "lab", "test", "security_people")
        + _registry_clips(root / "config/guardian/videos.json", root / "data/guardian/videos", "guardian", "validation", "wildlife")
        + bench_clips(root)
        + private_clips(storage_dir)
    )


def find_clip(storage_dir: Path, clip_id: str, root: Path = ROOT) -> LabClip | None:
    return next((clip for clip in all_clips(storage_dir, root) if clip.id == clip_id), None)
