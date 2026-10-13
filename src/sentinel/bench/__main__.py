"""Sentinel real-footage benchmark.

  python -m sentinel.bench run    --manifest config/bench/manifest.json --out bench_runs/NAME [--all | --clips a,b | --session ID] [--degrade low_light,rain]
  python -m sentinel.bench live   --config config/bench/live.example.json --duration 120 --out bench_runs/live_NAME
  python -m sentinel.bench report --runs bench_runs/NAME
  python -m sentinel.bench view   --runs bench_runs [--port 8090]
  python -m sentinel.bench export --session bench_runs/NAME/SESSION --clip CLIP --out annotated.mp4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .degrade import DEGRADATIONS
from .report import aggregate, write_reports


def _plan(manifest: dict, args: argparse.Namespace) -> list[tuple[str, list[str], str | None]]:
    degradations = [item for item in (args.degrade or "").split(",") if item]
    for item in degradations:
        if item not in DEGRADATIONS:
            raise SystemExit(f"unknown degradation {item!r}; choose from {', '.join(DEGRADATIONS)}")
    plan: list[tuple[str, list[str], str | None]] = []
    if args.clips:
        ids = args.clips.split(",")
        plan.append(("+".join(ids) if len(ids) > 1 else ids[0], ids, None))
    elif args.session:
        session = next((s for s in manifest.get("sessions", []) if s["id"] == args.session), None)
        if session is None:
            raise SystemExit(f"unknown session {args.session!r}")
        plan.append((session["id"], session["clips"], None))
    else:  # --all, or default
        plan += [(clip["id"], [clip["id"]], None) for clip in manifest["clips"] if Path(clip["video"]).exists()]
        plan += [(session["id"], session["clips"], None) for session in manifest.get("sessions", [])
                 if all(Path(manifest["_clips"][c]["video"]).exists() for c in session["clips"])]
        for item in manifest.get("degradation_runs", []):
            for kind in item["degradations"]:
                for clip_id in item["clips"]:
                    if Path(manifest["_clips"][clip_id]["video"]).exists():
                        plan.append((f"{clip_id}__{kind}", [clip_id], kind))
    if degradations:
        plan = [(f"{name}__{kind}", clips, kind) for name, clips, _ in plan for kind in degradations]
    return plan


def cmd_run(args: argparse.Namespace) -> None:
    from .runner import load_manifest, run_offline

    manifest = load_manifest(args.manifest)
    out = Path(args.out)
    plan = _plan(manifest, args)
    if not plan:
        raise SystemExit("nothing to run: are the clips downloaded? (python scripts/fetch_bench_footage.py)")
    results = []
    for name, clip_ids, degradation in plan:
        print(f"== {name}: {', '.join(clip_ids)}{' [' + degradation + ']' if degradation else ''}", flush=True)
        result = run_offline(
            manifest, clip_ids, out / name,
            degradation=degradation, max_seconds=args.max_seconds, reference=not args.no_reference,
            motion_gate=not args.no_motion_gate, session_id=name, detector_overrides=_detector_overrides(args),
        )
        for clip in result["clips"]:
            det = clip["detection"]
            print(
                f"   {clip['clip_id']}: {clip['frames_processed']} frames, {clip['throughput_fps']} fps"
                + (f", det P={det['precision']} R={det['recall']}" if det else ", unlabeled")
                + f", events {clip['event_counts']}, notifications {clip['notifications']}",
                flush=True,
            )
        results.append(result)
    _write(out, results)


def _detector_overrides(args: argparse.Namespace) -> dict:
    overrides = {}
    if args.model:
        overrides["model"] = args.model
    if args.score_threshold is not None:
        overrides["score_threshold"] = args.score_threshold
    if args.tiles:
        overrides["tiles"] = [int(value) for value in args.tiles.lower().split("x")]
    return overrides


def cmd_live(args: argparse.Namespace) -> None:
    from ..config import load_config
    from .live import run_live

    config = load_config(args.config)
    result = run_live(config, Path(args.out) / "live", args.duration)
    for clip in result["clips"]:
        print(f"{clip['clip_id']}: {clip['frames_processed']} frames, {clip['throughput_fps']} fps, latency p95 {clip['capture_to_decision_ms']['p95']} ms, dropped {clip['frames_dropped_by_reader']}, status {clip['camera_status']}")
    _write(Path(args.out), [result])


def cmd_report(args: argparse.Namespace) -> None:
    root = Path(args.runs)
    results = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*/result.json"))]
    if not results:
        raise SystemExit(f"no session results under {root}")
    _write(root, results)


def _write(out: Path, results: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    existing = {json.loads(p.read_text(encoding="utf-8"))["session_id"]: p for p in out.glob("*/result.json")}
    merged = {result["session_id"]: result for result in (json.loads(p.read_text(encoding="utf-8")) for p in existing.values())}
    merged.update({result["session_id"]: result for result in results})
    write_reports(aggregate(list(merged.values())), out)
    print(f"report: {out / 'report.md'}  |  {out / 'report.html'}")


def cmd_view(args: argparse.Namespace) -> None:
    import uvicorn

    from .viewer import create_viewer

    print(f"Viewer on http://127.0.0.1:{args.port} (local only)")
    uvicorn.run(create_viewer(args.runs), host="127.0.0.1", port=args.port, log_level="warning")


def cmd_export(args: argparse.Namespace) -> None:
    from .render import export_video

    print(export_video(Path(args.session), args.clip, Path(args.out)))


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="sentinel.bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Offline benchmark over recorded footage")
    run.add_argument("--manifest", default="config/bench/manifest.json")
    run.add_argument("--out", required=True)
    group = run.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Every clip, every session, and the manifest's degradation runs (default)")
    group.add_argument("--clips", help="Comma-separated clip ids to run together as one session")
    group.add_argument("--session", help="A session id from the manifest")
    run.add_argument("--degrade", help=f"Apply synthetic degradation(s): {', '.join(DEGRADATIONS)}")
    run.add_argument("--max-seconds", type=float)
    run.add_argument("--no-reference", action="store_true", help="Skip the annotation reference pass")
    run.add_argument("--no-motion-gate", action="store_true", help="Run the detector on every sampled frame")
    run.add_argument("--model", choices=["efficientdet_lite0", "efficientdet_lite2"], help="Override the detector model")
    run.add_argument("--score-threshold", type=float, help="Override the detector score threshold")
    run.add_argument("--tiles", help="Tiled detection grid, e.g. 2x2 (full frame is always included)")
    run.set_defaults(handler=cmd_run)

    live = commands.add_parser("live", help="Live streams (RTSP/HTTP/USB/paced files), all cameras at once")
    live.add_argument("--config", required=True)
    live.add_argument("--duration", type=float, default=120)
    live.add_argument("--out", required=True)
    live.set_defaults(handler=cmd_live)

    report = commands.add_parser("report", help="Rebuild report.md/html from session results")
    report.add_argument("--runs", required=True)
    report.set_defaults(handler=cmd_report)

    view = commands.add_parser("view", help="Visual playback/debugging of runs")
    view.add_argument("--runs", default="bench_runs")
    view.add_argument("--port", type=int, default=8090)
    view.set_defaults(handler=cmd_view)

    export = commands.add_parser("export", help="Render an annotated MP4 of one clip")
    export.add_argument("--session", required=True)
    export.add_argument("--clip", required=True)
    export.add_argument("--out", required=True)
    export.set_defaults(handler=cmd_export)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
