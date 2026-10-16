"""One-time export of MegaDetector V6 Apache (MDV6-apa-rtdetr-c, RT-DETRv2-R18,
classes animal/person/vehicle) to ONNX, so Sentinel runs it on its existing ONNX
Runtime path (`detectors_onnx`, DETR family) and never needs PyTorch.

Run in a separate environment with torch, torchvision, pyyaml, onnx, onnxruntime
(none are Sentinel dependencies), against the RT-DETRv2 sources vendored in
Pytorch-Wildlife (microsoft/Biodiversity, folder
PytorchWildlife/models/detection/rtdetr_apache/rtdetrv2_pytorch; Apache-2.0,
lyuwenyu/RT-DETR):

    python scripts/guardian/export_megadetector_onnx.py \
        --rtdetr-src <checkout>/PytorchWildlife/models/detection/rtdetr_apache/rtdetrv2_pytorch \
        --weights models/megadetector/download/MDV6-apa-rtdetr-c.pth \
        --out models/megadetector/mdv6_apa_rtdetr_c

The checkpoint is a pickle (Zenodo record 15398270); torch.load(weights_only=False)
executes pickle code, so the script refuses a file whose MD5 does not match the
one Zenodo publishes. Writes model.onnx (outputs `logits` [1,300,3] and
`pred_boxes` [1,300,4] cxcywh 0..1, input `images` [1,3,640,640] RGB 0..1) and
onnx_export.json (hashes, opset, parity with PyTorch)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ZENODO_MD5 = {
    "MDV6-apa-rtdetr-c.pth": ("3992888e26f627ee5a4ff9f116be97c3", "rtdetrv2_r18vd_120e_megadetector.yml"),
    "MDV6-apa-rtdetr-e.pth": ("6f8855d147ef17dc123c2f21fcb554b7", "rtdetrv2_r101vd_6x_megadetector.yml"),
}


def digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtdetr-src", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--parity-image", type=Path, help="optional real image for the parity check")
    args = parser.parse_args()

    import numpy as np
    import onnxruntime as ort
    import torch

    expected_md5, config_name = ZENODO_MD5[args.weights.name]
    md5 = digest(args.weights, "md5")
    if md5 != expected_md5:
        print(f"{args.weights}: MD5 {md5} does not match Zenodo's {expected_md5}; refusing to unpickle it")
        return 2
    sys.path.insert(0, str(args.rtdetr_src))
    from src.core import YAMLConfig  # noqa: E402  (vendored RT-DETRv2)

    config = args.rtdetr_src / "configs" / "rtdetrv2" / config_name
    cfg = YAMLConfig(str(config), PResNet={"pretrained": False})  # weights come from the checkpoint
    checkpoint = torch.load(args.weights, map_location="cpu", weights_only=False)
    state = checkpoint["ema"]["module"] if "ema" in checkpoint else checkpoint["model"]
    cfg.model.load_state_dict(state)
    model = cfg.model.deploy()

    class Raw(torch.nn.Module):
        """Model without the post-processor: Sentinel decodes logits/boxes itself."""

        def __init__(self) -> None:
            super().__init__()
            self.model = model

        def forward(self, images):
            out = self.model(images)
            return out["pred_logits"], out["pred_boxes"]

    raw = Raw().eval()
    if args.parity_image:
        from PIL import Image

        rgb = np.asarray(Image.open(args.parity_image).convert("RGB").resize((640, 640), Image.BILINEAR), dtype=np.float32) / 255.0
        sample = torch.from_numpy(rgb.transpose(2, 0, 1)[None].copy())
    else:
        sample = torch.rand(1, 3, 640, 640, generator=torch.Generator().manual_seed(0))
    args.out.mkdir(parents=True, exist_ok=True)
    onnx_path = args.out / "model.onnx"
    started = time.perf_counter()
    with torch.no_grad():
        torch.onnx.export(raw, (sample,), str(onnx_path), input_names=["images"], output_names=["logits", "pred_boxes"],
                          opset_version=args.opset, do_constant_folding=True, dynamo=False)
        ref_logits, ref_boxes = raw(sample)
    export_s = time.perf_counter() - started
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    logits, boxes = session.run(None, {"images": sample.numpy()})
    parity = {
        "max_logit_diff": float(np.abs(logits - ref_logits.numpy()).max()),
        "max_box_diff": float(np.abs(boxes - ref_boxes.numpy()).max()),
        "same_top_query": int(logits[0].max(1).argmax()) == int(ref_logits[0].max(1).values.argmax()),
        "input": str(args.parity_image.name) if args.parity_image else "random tensor (seed 0)",
    }
    record = {
        "model": args.weights.stem,
        "source": "https://zenodo.org/records/15398270 (Pytorch-Wildlife model weights)",
        "weights_md5": md5,
        "weights_sha256": digest(args.weights, "sha256"),
        "config": config_name,
        "onnx_sha256": digest(onnx_path, "sha256"),
        "opset": args.opset,
        "classes": ["animal", "person", "vehicle"],
        "input": {"name": "images", "shape": [1, 3, 640, 640], "format": "RGB float 0..1, resized (no letterbox)"},
        "outputs": {"logits": "[1,300,3] sigmoid scores", "pred_boxes": "[1,300,4] cxcywh normalised"},
        "parity": parity,
        "export_s": round(export_s, 1),
        "torch": torch.__version__,
        "onnxruntime": ort.__version__,
    }
    (args.out / "onnx_export.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0 if parity["max_logit_diff"] < 1e-2 and parity["max_box_diff"] < 1e-3 else 1


if __name__ == "__main__":
    sys.exit(main())
