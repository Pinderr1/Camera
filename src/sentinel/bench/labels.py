"""Ground-truth loaders. Every format is normalised to per-frame person boxes
with stable annotation ids; boxes the annotators marked as don't-care become
`ignore` regions (detections there are neither rewarded nor penalised)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import BBox

# MOT16 classes: 1 pedestrian (scored). 2 person on vehicle and 7 static person
# are real people the benchmark does not score; 8 distractor and 12 reflection
# are person-like but not people. Other classes are not person ground truth.
MOT_IGNORE_CLASSES = {2, 7, 8, 12}
MOT_NOT_A_PERSON = {8, 12}


@dataclass(frozen=True)
class GTBox:
    track_id: str
    label: str
    bbox: BBox
    ignore: bool = False
    visibility: float = 1.0
    attributes: tuple[str, ...] = ()
    real_person: bool = True  # False for annotated distractors/reflections


@dataclass
class GroundTruth:
    fps: float
    frames: dict[int, list[GTBox]]
    source: str
    notes: str = ""
    frame_count: int = 0
    behaviours: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)

    def at_frame(self, index: int) -> list[GTBox]:
        return self.frames.get(index, [])

    def track_ids(self) -> set[str]:
        return {box.track_id for boxes in self.frames.values() for box in boxes if not box.ignore}


def load_mot(path: str | Path, width: int, height: int, fps: float) -> GroundTruth:
    """MOTChallenge gt.txt: frame,id,x,y,w,h,consider,class,visibility (1-based frames)."""
    frames: dict[int, list[GTBox]] = {}
    last_frame = 0
    for line in Path(path).read_text().splitlines():
        parts = line.split(",")
        if len(parts) < 9:
            continue
        frame, track, x, y, w, h = int(parts[0]), parts[1], *map(float, parts[2:6])
        consider, cls, visibility = int(float(parts[6])), int(float(parts[7])), float(parts[8])
        if cls != 1 and cls not in MOT_IGNORE_CLASSES:
            continue
        ignore = cls in MOT_IGNORE_CLASSES or consider == 0
        index = frame - 1
        last_frame = max(last_frame, index)
        frames.setdefault(index, []).append(
            GTBox(
                f"mot{track}",
                "person",
                _normalise(x, y, w, h, width, height),
                ignore=ignore,
                visibility=visibility,
                real_person=cls not in MOT_NOT_A_PERSON,
            )
        )
    return GroundTruth(fps, frames, "mot", "MOTChallenge pedestrian boxes", frame_count=last_frame + 1)


def load_caviar(path: str | Path, width: int, height: int, fps: float) -> GroundTruth:
    """CAVIAR CVML: per-frame <object> boxes (xc, yc, w, h in pixels) with
    hypothesis labels. Entry/exit frames ("appear"/"disappear") are partial
    bodies at the image edge and are treated as don't-care."""
    root = ElementTree.parse(path).getroot()
    frames: dict[int, list[GTBox]] = {}
    behaviours: dict[str, list[tuple[int, int, str]]] = {}
    last_frame = 0
    for frame in root.iter("frame"):
        index = int(frame.get("number"))
        last_frame = max(last_frame, index)
        for obj in frame.iter("object"):
            box = obj.find("box")
            if box is None:
                continue
            w, h = float(box.get("w")), float(box.get("h"))
            x = float(box.get("xc")) - w / 2
            y = float(box.get("yc")) - h / 2
            appearance = (obj.findtext("appearance") or "").strip()
            role = (obj.findtext(".//role") or "").strip()
            situation = (obj.findtext(".//situation") or "").strip()
            context = (obj.findtext(".//context") or "").strip()
            label = "bag" if role == "leaving object" else "person"  # CAVIAR labels the left item itself this way
            track_id = f"cav{obj.get('id')}"
            ignore = appearance in {"appear", "disappear"} or w * h < 64
            frames.setdefault(index, []).append(
                GTBox(track_id, label, _normalise(x, y, w, h, width, height), ignore=ignore, attributes=(role, situation, context))
            )
            if situation:
                spans = behaviours.setdefault(track_id, [])
                if spans and spans[-1][2] == situation and spans[-1][1] == index - 1:
                    spans[-1] = (spans[-1][0], index, situation)
                else:
                    spans.append((index, index, situation))
    return GroundTruth(fps, frames, "caviar", "CAVIAR person boxes and behaviour labels", frame_count=last_frame + 1, behaviours=behaviours)


def load_labels(spec: dict[str, Any] | None, width: int, height: int, fps: float) -> GroundTruth | None:
    if not spec:
        return None
    kind, path = spec["format"], spec["path"]
    # Pixel-based labels keep the resolution they were drawn at, even if the video was re-encoded.
    width, height = int(spec.get("width", width)), int(spec.get("height", height))
    fps = float(spec.get("fps") or fps)
    if kind == "mot":
        return load_mot(path, width, height, fps)
    if kind == "caviar":
        return load_caviar(path, width, height, fps)
    if kind == "sentinel":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        frames: dict[int, list[GTBox]] = {}
        for row in data.get("boxes", []):
            frames.setdefault(int(row["frame"]), []).append(
                GTBox(str(row["id"]), row.get("label", "person"), tuple(row["bbox"]), ignore=bool(row.get("ignore", False)))
            )
        return GroundTruth(fps, frames, "sentinel", data.get("notes", ""), frame_count=max(frames, default=-1) + 1)
    raise ValueError(f"unknown label format {kind!r}")


def _normalise(x: float, y: float, w: float, h: float, width: int, height: int) -> BBox:
    x0, y0 = max(0.0, x / width), max(0.0, y / height)
    x1, y1 = min(1.0, (x + w) / width), min(1.0, (y + h) / height)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))
