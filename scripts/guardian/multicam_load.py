"""Multi-camera load test: several wildlife clips played as live cameras through
the real SentinelService (camera workers, motion gate, supervisor, health,
Guardian, SpeciesNet), to see what each detector configuration costs when
cameras share one GPU.

    python scripts/guardian/multicam_load.py --cameras 4 --seconds 90 \
        --detectors yolox_s,yolox_s+mdv6,scheduled --out bench_runs/guardian_multicam/NAME

Clips come from data/guardian/videos/ (scripts/guardian/fetch_wildlife_videos.py)
and loop, paced at their own frame rate; each camera asks for 5 fps. Reports per
configuration: processed fps per camera, detection and capture-to-decision
latency (p50/p95, from the workers' own metrics), frames skipped, camera health
states, species jobs dropped, process CPU, RSS and Windows GPU counters.
Numbers describe this machine only."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_eval import DETECTORS  # noqa: E402  (same named configurations as the clip test)

CLIPS = ["black_bear_day", "elk_herd_day", "river_empty", "fox_family_night", "cougar_day", "moose_night"]


def config(cameras: int, detector: dict, storage: str, species: bool) -> dict:
    return {
        "site": {"id": "multicam_load", "name": "Guardian multi-camera load"},
        "cameras": [{"id": f"cam{i + 1}", "name": CLIPS[i % len(CLIPS)], "fps": 5,
                     "source": {"type": "file", "path": str(ROOT / "data/guardian/videos" / f"{CLIPS[i % len(CLIPS)]}.webm"), "loop": True},
                     "zones": [{"id": f"near{i + 1}", "name": "Near", "type": "yard", "polygon": [[0, 0.7], [1, 0.7], [1, 1], [0, 1]]}]}
                    for i in range(cameras)],
        "topology": {"links": []}, "rules": [], "detector": detector,
        "categories": {"animal": {"detect": True, "alerts": False}},
        "guardian": {"enabled": True, "species_classifier": {"enabled": species, "model_dir": str(ROOT / "models/speciesnet/v4.0.3a")}},
        "privacy": {"store_clips": False}, "storage": {"dir": storage},
    }


def run(name: str, cameras: int, seconds: float, species: bool) -> dict:
    from sentinel.resources import _process_rss_mb
    from sentinel.service import SentinelService
    from speciesnet_eval import gpu_counters

    with tempfile.TemporaryDirectory(prefix="multicam_") as storage:
        service = SentinelService(config(cameras, DETECTORS[name], storage, species))
        service.start()
        time.sleep(min(20.0, seconds / 3))  # warm-up: model loads, first inferences
        cpu0, wall0 = sum(os.times()[:2]), time.perf_counter()
        rss, gpu = [], {}
        while time.perf_counter() - wall0 < seconds:
            time.sleep(5)
            rss.append(_process_rss_mb() or 0)
            if not gpu and time.perf_counter() - wall0 > seconds / 2:
                gpu = gpu_counters(os.getpid())
        wall = time.perf_counter() - wall0
        cpu_pct = round(100 * (sum(os.times()[:2]) - cpu0) / wall / (os.cpu_count() or 1), 1)
        status = {cid: dict(s) for cid, s in service.runtime.camera_status.items()}
        species_status = service.runtime.species.status() if service.runtime.species else None
        components = {k: v["state"] for k, v in service.runtime.health.components.items()}
        service.stop()
    per_camera = {}
    for cid, s in status.items():
        m = s.get("metrics") or {}
        per_camera[cid] = {"clip": next(c["name"] for c in config(cameras, {}, "", False)["cameras"] if c["id"] == cid),
                           "fps": s.get("fps"), "decoded_fps": s.get("decoded_fps"), "health": (s.get("health") or {}).get("state"),
                           "detect_ms_p50": m.get("detect_ms_p50"), "detect_ms_p95": m.get("detect_ms_p95"),
                           "latency_ms_p50": m.get("latency_ms_p50"), "latency_ms_p95": m.get("latency_ms_p95"),
                           "detector_state": s.get("detector_state"), "detector_errors": s.get("detector_errors")}
    return {"detector": name, "cameras": cameras, "seconds": round(wall, 1), "species": species, "per_camera": per_camera,
            "process_cpu_pct_of_machine": cpu_pct, "rss_mb_peak": max(rss) if rss else None, "gpu_mid_run": gpu,
            "species_worker": species_status, "components": components}


def render(results: list[dict]) -> str:
    lines = ["# Guardian multi-camera load", "",
             "Real SentinelService, looping wildlife clips paced as live cameras at 5 fps each. Detection and latency figures are the "
             "camera workers' own p50/p95 over their last 100 frames. Machine-specific.", "",
             "| Configuration | Cameras | Processed fps per camera (min-max) | Detect p50 / p95 ms (worst camera) | Capture-to-decision p50 / p95 ms (worst camera) | All monitoring | Species jobs dropped | CPU % of machine | RSS peak MB | GPU 3D+compute % |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        cams = list(r["per_camera"].values())
        fps = [c["fps"] or 0 for c in cams]
        worst = max(cams, key=lambda c: c["latency_ms_p95"] or 0)
        worst_detect = max(cams, key=lambda c: c["detect_ms_p95"] or 0)
        monitoring = sum(c["health"] == "monitoring" for c in cams)
        dropped = (r["species_worker"] or {}).get("dropped", "-")
        lines.append(f"| {r['detector']} | {r['cameras']} | {min(fps):.1f}-{max(fps):.1f} | {worst_detect['detect_ms_p50']} / {worst_detect['detect_ms_p95']} | "
                     f"{worst['latency_ms_p50']} / {worst['latency_ms_p95']} | {monitoring}/{len(cams)} | {dropped} | {r['process_cpu_pct_of_machine']} | "
                     f"{r['rss_mb_peak']} | {r['gpu_mid_run'].get('engine_utilization_pct', '-')} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cameras", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--detectors", default="yolox_s,yolox_s+mdv6,scheduled")
    parser.add_argument("--no-species", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "bench_runs/guardian_multicam/latest")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for name in args.detectors.split(","):
        results.append(run(name, args.cameras, args.seconds, not args.no_species))
        print(json.dumps(results[-1])[:600], flush=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (args.out / "report.md").write_text(render(results), encoding="utf-8")
    print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
