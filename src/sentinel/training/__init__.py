"""Training workflow: from Test Lab errors to a compared, reversible model change
(docs/training.md). Nothing here trains a model; training is a separate,
approved step. Everything stays on this computer."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def status(storage_dir: str | Path) -> dict[str, Any]:
    from .examples import personalization
    from .registry import ModelRegistry

    storage = Path(storage_dir)
    corrections: Counter = Counter()
    runs = 0
    for path in (storage / "lab" / "runs").glob("*/corrections.jsonl"):
        runs += 1
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                corrections[json.loads(line)["kind"]] += 1
    try:
        registry = ModelRegistry(storage).view()
    except (OSError, ValueError) as error:
        registry = {"error": str(error)}
    return {"corrections": dict(corrections), "runs_with_corrections": runs, "registry": registry,
            "personalization": personalization(storage),
            "commands": {
                "collect": "python -m sentinel.training collect --out bench_runs/examples [--purpose training]",
                "plan": "python -m sentinel.training plan bench_runs/examples",
                "register": "python -m sentinel.training register ID PATH.onnx --family yolox --labels coco80 --licence ... --training-data ...",
                "compare": "python -m sentinel.training compare --candidate ID --split test",
                "promote": "python -m sentinel.training promote ID",
                "rollback": "python -m sentinel.training rollback",
            }}
