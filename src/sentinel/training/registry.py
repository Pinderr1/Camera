"""Model registry with promotion and rollback (stdlib).

<storage>/models/registry.json records every detector Sentinel may run: the
built-in profiles and candidates registered by the owner (a fine-tuned or
newly evaluated ONNX file). Files are hashed at registration and never
deleted or overwritten by Sentinel. Promotion requires a recorded comparison
on the independent `test` split that the candidate does not lose, and a
commercially usable licence; the previous model stays registered, so rollback
is one command. Promoting changes the registry's `active` pointer and gives the
`detector` block to put in the site config (applied through the Setup editor,
which validates and audits it); it never edits the live config by itself."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

FAMILIES = {"yolox", "detr"}
LABEL_SETS = {"coco80", "coco91", "megadetector"}


def registry_path(storage_dir: str | Path) -> Path:
    return Path(storage_dir) / "models" / "registry.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class ModelRegistry:
    def __init__(self, storage_dir: str | Path) -> None:
        self.path = registry_path(storage_dir)
        self.data: dict[str, Any] = {"models": {}, "active": "current", "history": []}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def active(self) -> str:
        return self.data.get("active", "current")

    def register(self, model_id: str, model_path: str | Path, *, family: str, labels: str, input_size: list[int],
                 licence: str, training_data: str, commercial_ok: bool, base: str = "", notes: str = "", score_threshold: float = 0.5) -> dict[str, Any]:
        from ..lab.profiles import PROFILES

        if not model_id.replace("_", "").isalnum() or model_id in PROFILES:
            raise ValueError("model id must be letters, digits and underscores, and not a built-in profile")
        if model_id in self.data["models"]:
            raise ValueError(f"{model_id} is already registered (registered models are never replaced; use a new id)")
        if family not in FAMILIES or labels not in LABEL_SETS:
            raise ValueError(f"family must be one of {sorted(FAMILIES)}, labels one of {sorted(LABEL_SETS)}")
        if not licence or not training_data:
            raise ValueError("licence and training_data are required (check weights separately from code)")
        path = Path(model_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        entry = {
            "id": model_id, "path": str(path), "sha256": sha256(path), "family": family, "labels": labels,
            "input_size": [int(v) for v in input_size], "score_threshold": float(score_threshold), "licence": licence,
            "training_data": training_data, "commercial_ok": bool(commercial_ok), "base": base, "notes": notes,
            "status": "candidate", "registered": datetime.now().isoformat(timespec="seconds"), "evaluations": [],
        }
        self.data["models"][model_id] = entry
        self.save()
        return entry

    def detector(self, model_id: str) -> dict[str, Any]:
        from ..lab.profiles import PROFILES

        if model_id in PROFILES:
            return dict(PROFILES[model_id]["detector"])
        entry = self.data["models"][model_id]
        return {"type": "onnx", "model": entry["id"], "model_path": entry["path"], "family": entry["family"], "labels": entry["labels"],
                "input_size": entry["input_size"], "score_threshold": entry["score_threshold"]}

    def verify(self, model_id: str) -> bool:
        entry = self.data["models"][model_id]
        return Path(entry["path"]).is_file() and sha256(Path(entry["path"])) == entry["sha256"]

    def record_evaluation(self, model_id: str, comparison: dict[str, Any]) -> None:
        if model_id in self.data["models"]:
            self.data["models"][model_id]["evaluations"].append(comparison)
            self.save()

    def promote(self, model_id: str, actor: str, *, reason: str = "") -> dict[str, Any]:
        from ..lab.profiles import PROFILES

        if model_id not in PROFILES:
            entry = self.data["models"].get(model_id)
            if entry is None:
                raise KeyError(model_id)
            if not entry["commercial_ok"]:
                raise ValueError(f"{model_id}: licence or training data not cleared for commercial use")
            if not self.verify(model_id):
                raise ValueError(f"{model_id}: file missing or changed since registration")
            passed = [e for e in entry["evaluations"] if e.get("split") == "test" and e.get("verdict") in {"better", "no_worse"}]
            if not passed:
                raise ValueError(f"{model_id}: needs a comparison on the test split that it does not lose (python -m sentinel.training compare)")
            entry["status"] = "active"
        previous = self.active
        if previous in self.data["models"]:
            self.data["models"][previous]["status"] = "previous"
        self.data["active"] = model_id
        self.data["history"].append({"active": model_id, "previous": previous, "actor": actor, "reason": reason,
                                     "at": datetime.now().isoformat(timespec="seconds")})
        self.save()
        return {"active": model_id, "previous": previous, "detector": self.detector(model_id)}

    def rollback(self, actor: str) -> dict[str, Any]:
        if not self.data["history"]:
            raise ValueError("nothing to roll back")
        last = self.data["history"][-1]
        target = last["previous"]
        if target in self.data["models"] and not self.verify(target):
            raise ValueError(f"{target}: file missing or changed; cannot roll back to it")
        if self.active in self.data["models"]:
            self.data["models"][self.active]["status"] = "candidate"
        self.data["active"] = target
        self.data["history"].append({"active": target, "previous": last["active"], "actor": actor, "reason": "rollback",
                                     "at": datetime.now().isoformat(timespec="seconds")})
        self.save()
        return {"active": target, "detector": self.detector(target)}

    def view(self) -> dict[str, Any]:
        return {"active": self.active, "history": self.data["history"][-10:],
                "models": [{k: v for k, v in m.items() if k != "path"} | {"file": Path(m["path"]).name} for m in self.data["models"].values()]}


def lab_profiles(storage_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Registered candidates as Test Lab profiles."""
    try:
        registry = ModelRegistry(storage_dir)
    except (OSError, ValueError):
        return {}
    out = {}
    for model_id, entry in registry.data["models"].items():
        out[model_id] = {"name": f"{model_id} (candidate)" if entry["status"] != "active" else f"{model_id} (active)",
                         "description": f"{entry['licence']}; trained on {entry['training_data']}", "candidate": True,
                         "detector": registry.detector(model_id), "requires": []}
    return out
