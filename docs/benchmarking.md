# Real-footage benchmarking

`sentinel.bench` runs recorded or live camera footage through the **production pipeline** (privacy mask → motion gate → detector → tracker → temporal engine → incidents → alert policy) and measures accuracy where human annotations exist, plus latency, throughput, CPU, and memory everywhere. It is how detection or rule changes are judged. Simulated scenarios (`sentinel.replay`) test logic; this tests reality.

## Footage

```powershell
python scripts/fetch_bench_footage.py      # about 600 MB into data/bench/ (git-ignored)
```

The MOT16 frames are re-encoded with ffmpeg (libx264, CRF 20). Without ffmpeg on PATH, set `FFMPEG=<path to ffmpeg.exe>` or `pip install imageio-ffmpeg` (its bundled binary is used; nothing is installed system-wide). The footage is per machine: copy `data/bench/` between computers or fetch it again.

| Clip(s) | Source and licence | Annotations | Conditions |
| --- | --- | --- | --- |
| `mot16_02` | MOTChallenge MOT16-02, CC BY-NC-SA 3.0 | Per-frame pedestrian boxes and identities | Day, eye-level plaza, crowded |
| `mot16_04` | MOT16-04 | Same | **Night**, artificial light, elevated street camera, crowded |
| `mot16_09` | MOT16-09 | Same | Day, doorbell-height storefront |
| `caviar_*` (5 clips) | CAVIAR (EC IST 2001 37540), research use with acknowledgement | Per-frame boxes, identities, behaviour labels | Indoor lobby, overhead fisheye, 384x288, dim |
| `vtest` | OpenCV sample data | **None.** Visual review of sampled frames only | Day, outdoor campus path, ordinary activity |
| `mot16_0X_sub` | The MOT16 clips re-encoded to 640x360 at 10 fps by the fetcher | Same annotations, mapped by time (`labels.fps`, `labels.width/height`) | Camera sub-stream quality |

All of this is evaluation-only data (non-commercial licences). Never train shipped models on it. Rain and snow have no real labeled footage here. `--degrade low_light|night_ir|rain` applies synthetic degradations to real footage as a stress test, and reports always label those results `synthetic_*`.

## What is measured

| Metric | How | Needs labels |
| --- | --- | --- |
| Detection precision / recall / F1 | Person boxes at IoU ≥ 0.5 against annotations, on frames where the detector ran. MOT "don't-care" boxes (static people, distractors, reflections, low-visibility flags) neither reward nor penalise | Yes |
| Recall by size and by visibility | Annotation height bucket (small < 10% of frame height, medium < 25%, large), and recall for people at least 50% visible | Yes |
| Tracking continuity | Annotated identities mostly tracked (≥80% of frames), mostly lost (<20%), identity switches, tracks per identity | Yes |
| Occlusion and merging | Recall by visibility band (<25%, 25–50%, 50–75%, ≥75%). "Group boxes": false boxes containing the centres of two or more annotated people (several people reported as one) | Yes |
| Events vs expected | **Reference pass:** annotated boxes and identities run through the same zones and rules give the events Sentinel *should* produce with perfect perception. Hand-written `expected_events` in the manifest are added. Matching uses type, camera, zone, and a time window | Yes |
| Missed events / false alert events | Unmatched expected / unmatched produced events, per type | Yes |
| Notifications | Produced vs reference notifications within 15 s. False alerts per hour. For `expect_no_notifications` clips, every push counts as false | Yes, or a stated expectation |
| Event latency | Produced minus expected time per matched event | Yes |
| Stage latency | Decode, detect, track, engine: p50 / p95 / max, in ms | No |
| Throughput | Frames per second processed; realtime factor (video seconds ÷ wall seconds) | No |
| CPU / memory | Process CPU (share of one core and of the whole machine) and resident memory, via psutil | No |
| Live only | Capture-to-decision latency, frames dropped by the latest-frame reader, camera status | No |

## Commands

```powershell
$env:PYTHONPATH='src'
python -m sentinel.bench run --all --out bench_runs/NAME                         # every clip, sessions, degradations
python -m sentinel.bench run --clips mot16_04 --out bench_runs/night             # one clip
python -m sentinel.bench run --session street_3cam --out bench_runs/load         # simultaneous feeds
python -m sentinel.bench run --clips mot16_02 --degrade rain --out bench_runs/x  # synthetic stress
python -m sentinel.bench run --all --model efficientdet_lite2 --out bench_runs/lite2   # compare detectors
python -m sentinel.bench run --clips mot16_09 --tiles 2x2 --out bench_runs/tiles      # tiled detection
python -m sentinel.bench run --clips mot16_02 --model yolox_m --input-size 1280x736 --providers dml,cpu --out bench_runs/m1280
bash scripts/bench_detectors.sh bench_runs/detectors                            # every detector config, same clips and settings
python -m sentinel.bench compare bench_runs/detectors/* --out comparison.md      # one row per configuration
python -m sentinel.bench live --config config/bench/live.example.json --duration 60 --out bench_runs/live
python -m sentinel.bench view --runs bench_runs                                  # http://127.0.0.1:8090
python -m sentinel.bench export --session bench_runs/NAME/mot16_04 --clip mot16_04 --out night.mp4
```

Each run writes `report.md`, `report.html`, `report.json`, and per session `result.json`, `site_config.json` (credentials redacted), `clips/<id>/frames.jsonl` (per-frame detections, tracks, timings), and `clips/<id>/events.json` (produced, expected, matches, notifications).

### Viewer

`python -m sentinel.bench view` serves a local-only page with:
- Frame-by-frame playback with toggleable layers: zones, annotations (green; grey = don't-care), raw detections (yellow), and confirmed tracks (coloured, labelled with track id and site-track id).
- A timeline strip comparing expected events (top) with produced events (bottom). Red marks a miss or a false event.
- Clickable lists of expected events, produced events, and notifications that jump to the frame.
- Per-clip metrics.

### Testing live streams without a camera

Publish recordings as real RTSP streams with [MediaMTX](https://github.com/bluenviron/mediamtx) (MIT) and ffmpeg:

```powershell
.\mediamtx.exe                               # RTSP server on :8554
ffmpeg -re -stream_loop -1 -i data/bench/mot16/MOT16-02/MOT16-02.mp4 -c copy -f rtsp rtsp://127.0.0.1:8554/mot16_02
# repeat for mot16_04 and mot16_09, then:
python -m sentinel.bench live --config config/bench/live.example.json --duration 60 --out bench_runs/live
```

For a real camera, copy `live.example.json` and set `{"type": "rtsp", "url": "rtsp://user:${CAM_PASS}@IP:554/stream2"}` or `{"type": "usb", "index": 0}`.

## Adding your own footage

1. Put the recording in `data/bench/own/` (git-ignored). Add a camera with zones to `config/bench/site.json` and a clip entry to `config/bench/manifest.json`, with `conditions` tags such as `night`, `rain`, `ir`, or `ordinary_activity`.
2. Labels, from cheapest to strongest:
   - `"expect_no_notifications": true` for footage of ordinary activity you have reviewed. Any push then counts as a false alert.
   - `"expected_events": [{"event_type": "zone_entry", "zone_id": "...", "time_s": 12.5}]` written by hand while watching the clip.
   - Box annotations in Sentinel JSON (`{"boxes": [{"frame": 0, "id": "p1", "bbox": [x, y, w, h]}]}`, normalized), or MOT `gt.txt` exported from CVAT. These enable detection, tracking, and derived-event metrics.
3. State where the labels came from in `label_source`. Reports print it next to every number.

## Findings

See `ROADMAP.md`, "Real-footage benchmark findings", for the current numbers and limitations.

### YOLOX-s in fp16 (2026-10-16)

Machine: i5-1135G7 laptop, Intel Iris Xe, DirectML (ONNX Runtime 1.24). Footage restored on this machine with the fetcher (the 2026-10-13 comparison ran on the i7-2600K / GTX 1070 desktop). Same five clips, model (YOLOX-s), input (960×544), threshold (0.5), provider and pipeline as the detector comparison; the only difference is the ONNX file: `yolox_s.dynamic.onnx` (fp32) vs `yolox_s.dynamic.fp16.onnx` (`scripts/convert_onnx_fp16.py`, fp32 inputs and outputs). The fp32 figures reproduce the 2026-10-13 accuracy table exactly. Runs: `bench_runs/people_fp16/{fp32,fp16}` (git-ignored).

| Clip | Precision fp32 → fp16 | Recall fp32 → fp16 | Recall ≥50 % visible | False positives | Missed | Mostly tracked | ID switches | Detect p50 / p95 ms fp32 → fp16 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MOT16-09 (doorbell height) | 94.6 → 94.5 % | 74.1 → 74.1 % | 97.5 → 97.7 % | 37 → 38 | 228 → 228 | 10/24 → 10/24 | 50 → 43 | 190/259 → 167/323 |
| MOT16-02 (crowded plaza) | 87.8 → 87.7 % | 32.0 → 31.9 % | 65.4 → 65.2 % | 132 → 133 | 2017 → 2019 | 6/51 → 6/51 | 49 → 48 | 180/219 → 143/250 |
| MOT16-04 (night, elevated) | 98.0 → 98.1 % | 53.4 → 53.2 % | 79.6 → 79.4 % | 84 → 82 | 3692 → 3703 | 19/82 → 19/82 | 71 → 69 | 196/280 → 142/194 |
| CAVIAR Browse1 | 87.3 → 87.0 % | 15.7 → 15.4 % | 15.7 → 15.4 % | 7 → 7 | 258 → 259 | 0/4 → 0/4 | 1 → 1 | 207/305 → 128/163 |
| CAVIAR Walk1 | 87.5 → 87.5 % | 12.5 → 12.5 % | 12.5 → 12.5 % | 3 → 3 | 147 → 147 | 0/4 → 0/4 | 0 → 0 | 191/252 → 119/148 |

Small and distant people: these annotations have no box under 10 % of the frame height, so the "small" bucket is empty; medium-sized people (10–25 %) move by at most 0.9 points (CAVIAR Browse1 42.6 → 41.7 %, MOT16-02 33.3 → 33.0 %, others unchanged).

Detector only (MOT16-09, 525 frames, streamed, two alternating runs each): p50 234 / 241 ms (fp32) vs 161 / 177 ms (fp16), about 30 % faster; shared GPU memory 282 MB vs 105 MB. Process CPU stays at 7–10 % of the machine either way. Single-sample GPU utilisation readings varied too much between repeats to compare.

Verdict: on this DirectML GPU, fp16 gives the same detections within 0.3 points of precision and recall and about 30 % less detection time. It is available as `detector.precision: "fp16"` (opt-in; it needs the converted file, uses fp32 on CPU, and falls back to fp32 when the copy is missing or fails to load). MegaDetector V6 does **not** load in fp16 under DirectML (graph fusion error with every op block list tried), so it stays fp32. Not measured on the GTX 1070 or on CUDA.
