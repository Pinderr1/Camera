"""Detector configurations the Test Lab can compare (stdlib).

A profile is a `detector` config block exactly as a site config would hold it,
so a profile that wins in the lab can be copied into the live config unchanged.
Profiles whose model file is missing are listed as unavailable, never faked.
Candidates registered with `python -m sentinel.training register` appear here too."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .catalog import ROOT

MD = "mdv6_apa_rtdetr_c"

PROFILES: dict[str, dict[str, Any]] = {
    "current": {
        "name": "Sentinel default (model: auto)",
        "description": "What a new installation runs: YOLOX-s at 960 on a GPU, YOLOX-tiny at 416 on CPU.",
        "detector": {"type": "onnx", "model": "auto"},
        "requires": [],
    },
    "yolox_s": {
        "name": "YOLOX-s 960",
        "description": "Default GPU detector (Apache-2.0, COCO).",
        "detector": {"type": "onnx", "model": "yolox_s", "input_size": [960, 544], "score_threshold": 0.5},
        "requires": ["models/onnx/yolox_s.onnx"],
    },
    "yolox_s_fp16": {
        "name": "YOLOX-s 960 fp16",
        "description": "Same model in half precision (opt-in `precision`): about 30 % faster on DirectML.",
        "detector": {"type": "onnx", "model": "yolox_s", "input_size": [960, 544], "score_threshold": 0.5, "precision": "fp16"},
        "requires": ["models/onnx/yolox_s.onnx"],
    },
    "yolox_tiny": {
        "name": "YOLOX-tiny 416",
        "description": "CPU profile: fast, misses small and distant objects.",
        "detector": {"type": "onnx", "model": "yolox_tiny", "input_size": [416, 416], "score_threshold": 0.45},
        "requires": ["models/onnx/yolox_tiny.onnx"],
    },
    "yolox_s_megadetector": {
        "name": "YOLOX-s + MegaDetector V6 (animals)",
        "description": "Guardian add-on: MegaDetector V6 Apache adds animal boxes YOLOX missed (about 2x detection time).",
        "detector": {"type": "onnx", "model": "auto", "animal_detector": {"model": MD, "score_threshold": 0.6}},
        "requires": [f"models/megadetector/{MD}/model.onnx"],
    },
    "megadetector": {
        "name": "MegaDetector V6 alone",
        "description": "Camera-trap detector (animal / person / vehicle only). Comparison only.",
        "detector": {"type": "onnx", "model": MD, "score_threshold": 0.6},
        "requires": [f"models/megadetector/{MD}/model.onnx"],
    },
}


def available(profile: dict[str, Any], root: Path = ROOT) -> bool:
    return all((root / path).exists() for path in profile.get("requires", []))


def list_profiles(extra: dict[str, dict[str, Any]] | None = None, root: Path = ROOT) -> list[dict[str, Any]]:
    out = []
    for key, profile in (PROFILES | (extra or {})).items():
        out.append({"id": key, "name": profile["name"], "description": profile.get("description", ""),
                    "available": available(profile, root), "candidate": bool(profile.get("candidate")),
                    "detector": profile["detector"]})
    return out


def detector_config(profile_id: str, extra: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    profiles = PROFILES | (extra or {})
    if profile_id not in profiles:
        raise KeyError(f"unknown detector profile {profile_id!r}")
    detector = copy.deepcopy(profiles[profile_id]["detector"])
    detector.setdefault("min_box_area", 0.0004)
    return detector
