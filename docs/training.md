# Training workflow

From Test Lab errors to a compared, reversible model change. Sentinel itself never trains a model and never changes its live detector on its own. Everything below stays on this computer.

```powershell
$env:PYTHONPATH='src'
python -m sentinel.training status                                   # corrections, registry, personalization
python -m sentinel.training collect --out bench_runs/examples        # errors + corrections -> examples (evaluation)
python -m sentinel.training collect --out bench_runs/train --purpose training
python -m sentinel.training check bench_runs/train                   # splits disjoint, boxes valid, files present
python -m sentinel.training plan bench_runs/train                    # fine-tuning plan; does not train
python -m sentinel.training register cand1 path\to\model.onnx --family yolox --labels coco80 --input-size 960,544 `
    --licence "Apache-2.0" --training-data "COCO 2017 + owner corrections" --commercial-ok
python -m sentinel.training compare --candidate cand1 --split test --out bench_runs/compare_cand1
python -m sentinel.training promote cand1 --by NAME                  # only after a test-split comparison it does not lose
python -m sentinel.training rollback --by NAME
python -m sentinel.training personalization on --by NAME             # opt in to using your own footage (local)
```

Add `--storage DIR` or `--config FILE` when Sentinel's storage is not `data/sentinel`.

## 1. Find errors

The Test Lab marks automatic errors where labels exist (person boxes with no labelled person, labelled people with no box, frames where a labelled subject has no box or an empty scene has one). You add your own: wrong boxes, wrong labels, missed objects (drawn), and right or false-alarm events.

## 2. Collect examples

`collect` gathers both into `examples.jsonl` (one line per error or correction, with run, clip, split, frame, kind and origin), copies the frames, and writes `coco_<split>.json` for every frame you reviewed: the detector's confident boxes, minus the ones you marked wrong, relabelled where you corrected them, plus the boxes you drew. `manifest.json` records each clip's licence and attribution and what was excluded and why.

Rules for `--purpose training`:

- the **test split is never exported** (it must stay independent for `compare`);
- a clip whose licence does not allow commercial training is excluded (MOT16 CC BY-NC-SA, CAVIAR research use, unverified public-domain claims);
- your own footage is included only while **personalization** is on. Turning it on records who agreed and when; turning it off stops new exports. Footage and exports stay on this computer.

## 3. Choose and evaluate models

Candidate models must have commercially usable code *and* weights (sources.md, `detectors_onnx.MODELS`): YOLOX, RT-DETRv2, D-FINE (COCO-only weights), MegaDetector V6 Apache. Excluded: Ultralytics (AGPL), YOLOv6/7/9 (GPL), Objects365-trained weights until legal review. The lab's model menu lists the installed built-ins and every registered candidate, so a candidate can be watched on any clip.

`compare` runs baseline and candidate on every labelled clip of a split through the Lab's runner (same rules, rate and Guardian settings) and keeps both runs, so you can replay each difference side by side. Verdict: **better** (more found, not more false positives), **no_worse** (within one point / one frame), **worse**, or **insufficient_labels**. The new public test clips are unlabelled at first: label them (present/absent ranges in `config/lab/videos.json`, or boxes through corrections) before relying on a test-split verdict.

## 4. Fine-tune (prepared, not run)

`plan` reports what the reviewed data supports and what blocks training (fewer than 200 boxes per class, an evaluation-only export, a failed check). The recipe: the official YOLOX training code (Apache-2.0) from the shipped COCO weights, backbone frozen, 10-30 epochs, mixed 1:1 with COCO images so it does not forget, on a CUDA GPU. Export to ONNX, then `register` and `compare`. **Training needs the owner's approval** and is not started by any Sentinel command.

## 5. Promote and roll back

`register` hashes the file (SHA-256) and never replaces an existing entry. `promote` refuses a candidate without a commercially usable licence, with a missing or changed file, or without a test-split comparison that it does not lose. It records who promoted it and prints the `detector` block for the site config. Apply that block in the Setup editor, which validates and audits it. `rollback` returns to the previous model; nothing is deleted. The registry lives in `<storage>/models/registry.json`.
