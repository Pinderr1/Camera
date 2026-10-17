"""One-time half-precision copy of an ONNX detector for `detector.precision: fp16`.

Run in a separate environment with `onnx` and `onnxconverter-common` (MIT; not
Sentinel dependencies):

    python scripts/convert_onnx_fp16.py models/onnx/yolox_s.dynamic.onnx

Writes `<model>.fp16.onnx` next to the input, with fp32 inputs and outputs so
Sentinel's pre- and post-processing are unchanged. Convert the file the detector
actually loads: for YOLOX at a non-default size (960x544) that is the
resolution-flexible `*.dynamic.onnx` copy Sentinel writes on first use.

Measured (docs/benchmarking.md, 2026-10-16): YOLOX-s works on DirectML in fp16;
MegaDetector V6 (RT-DETR) does not load under DirectML in fp16 (graph fusion
error), so keep it in fp32."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    args = parser.parse_args()
    import onnx
    from onnxconverter_common import float16

    model = onnx.load(str(args.model))
    half = float16.convert_float_to_float16(model, keep_io_types=True)
    target = args.model.with_name(f"{args.model.stem}.fp16.onnx")
    onnx.save(half, str(target))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"wrote {target} ({target.stat().st_size / 1e6:.1f} MB, SHA-256 {digest[:16]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
