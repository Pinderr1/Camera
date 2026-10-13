#!/usr/bin/env bash
# Detector comparison: every configuration runs the same 5 annotated clips through
# the full pipeline with identical benchmark settings. Thresholds are each model's
# operating point from a score sweep on sampled frames of the same footage.
# Usage: PYTHONPATH=src bash scripts/bench_detectors.sh bench_runs/detectors ["name1 name2 ..."]
set -u
OUT=${1:-bench_runs/detectors}
ONLY=${2:-}
CLIPS="mot16_09 mot16_02 mot16_04 caviar_browse1 caviar_walk1"
run() {
  local name=$1; shift
  if [ -n "$ONLY" ] && [[ " $ONLY " != *" $name "* ]]; then return; fi
  for clip in $CLIPS; do
    python -u -m sentinel.bench run --clips "$clip" --out "$OUT/$name" "$@" 2>&1 \
      | grep --line-buffered -vE "XNNPACK|clearcut|Source Location|playlog|W0000|I0000|report:"
  done
}
run lite0           --model efficientdet_lite0 --score-threshold 0.45
run lite2           --model efficientdet_lite2 --score-threshold 0.45
run yolox_tiny_640  --model yolox_tiny --input-size 640x384 --score-threshold 0.45 --providers dml,cpu
run yolox_s_640     --model yolox_s --score-threshold 0.5 --providers dml,cpu
run yolox_s_960     --model yolox_s --input-size 960x544 --score-threshold 0.5 --providers dml,cpu
run yolox_m_960     --model yolox_m --input-size 960x544 --score-threshold 0.45 --providers dml,cpu
run yolox_m_1280    --model yolox_m --input-size 1280x736 --score-threshold 0.5 --providers dml,cpu
run yolox_s_tiles   --model yolox_s --tiles 2x2 --score-threshold 0.55 --providers dml,cpu
run rtdetrv2_640    --model rtdetrv2_r18 --score-threshold 0.55 --providers dml,cpu
run dfine_s_cpu     --model dfine_s --score-threshold 0.55 --providers cpu
run rfdetr_base     --model rfdetr_base --score-threshold 0.45 --providers dml,cpu
# Tracking variant: weak boxes (>= 0.25) may extend existing tracks (ByteTrack-style).
run yolox_s_960_low  --model yolox_s --input-size 960x544 --score-threshold 0.5 --low-score-threshold 0.25 --providers dml,cpu
run yolox_m_960_low  --model yolox_m --input-size 960x544 --score-threshold 0.45 --low-score-threshold 0.25 --providers dml,cpu
echo ALL_DONE
