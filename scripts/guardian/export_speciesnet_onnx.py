"""One-time export of Google SpeciesNet (PyTorch crop classifier) to ONNX so
Sentinel can run it with its existing ONNX Runtime (DirectML/CUDA/CPU) and
never needs PyTorch or MegaDetector at run time.

Run in a separate environment that has torch, onnx2torch and onnx (they are not
Sentinel dependencies):

    python scripts/guardian/export_speciesnet_onnx.py models/speciesnet/v4.0.3a

The checkpoint is a pickle produced by Google (Kaggle google/speciesnet,
Apache-2.0); torch.load(weights_only=False) executes pickle code, so only load
archives whose SHA-256 matches the one recorded in docs/guardian-datasets.md.
Writes speciesnet.onnx and onnx_export.json (hashes, opset, parity check).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

IMG_SIZE = 480


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    import numpy as np
    import torch

    info = json.loads((args.model_dir / "info.json").read_text(encoding="utf-8"))
    if info.get("type") != "always_crop":
        print("expected the always_crop (crop classifier) variant", file=sys.stderr)
        return 2
    checkpoint = args.model_dir / info["classifier"]
    model = torch.load(checkpoint, map_location="cpu", weights_only=False)
    model.eval()
    dummy = torch.rand(1, IMG_SIZE, IMG_SIZE, 3)  # NHWC float in 0..1, as SpeciesNet feeds it
    out_path = args.model_dir / "speciesnet.onnx"
    torch.onnx.export(
        model, (dummy,), str(out_path), input_names=["image"], output_names=["logits"],
        dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}}, opset_version=args.opset,
        dynamo=False,
    )

    import onnxruntime as ort

    session = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
    batch = torch.rand(2, IMG_SIZE, IMG_SIZE, 3)
    with torch.no_grad():
        expected = model(batch).numpy()
    started = time.perf_counter()
    got = session.run(None, {"image": batch.numpy()})[0]
    elapsed = time.perf_counter() - started
    max_diff = float(np.abs(expected - got).max())
    same_top1 = bool((expected.argmax(-1) == got.argmax(-1)).all())
    labels = (args.model_dir / info["classifier_labels"]).read_text(encoding="utf-8").splitlines()
    record = {
        "source_checkpoint": checkpoint.name,
        "source_sha256": sha256(checkpoint),
        "onnx": out_path.name,
        "onnx_sha256": sha256(out_path),
        "opset": args.opset,
        "speciesnet_version": info.get("version"),
        "labels": info["classifier_labels"],
        "num_labels": len(labels),
        "output_classes": int(got.shape[-1]),
        "input": "NHWC float32 0..1, 480x480 RGB crop",
        "parity_max_abs_logit_diff": max_diff,
        "parity_same_top1": same_top1,
        "cpu_seconds_batch2": round(elapsed, 3),
        "torch": torch.__version__,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (args.model_dir / "onnx_export.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0 if same_top1 and len(labels) == got.shape[-1] else 1


if __name__ == "__main__":
    sys.exit(main())
