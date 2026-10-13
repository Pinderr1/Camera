# Real-footage benchmarking

`sentinel.bench` runs recorded or live camera footage through the **production pipeline** (privacy mask → motion gate → detector → tracker → temporal engine → incidents → alert policy) and measures accuracy where human annotations exist, plus latency, throughput, CPU, and memory everywhere. It is how detection or rule changes are judged. Simulated scenarios (`sentinel.replay`) test logic; this tests reality.

## Footage

```powershell
python scripts/fetch_bench_footage.py      # about 600 MB into data/bench/ (git-ignored)
```

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
