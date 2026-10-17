"""python -m sentinel.training ... (docs/training.md)"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _storage(args) -> Path:
    if args.storage:
        return Path(args.storage)
    if args.config:
        from ..config import load_config

        return Path(load_config(args.config, resolve_secrets=False).get("storage", {}).get("dir", "data/sentinel"))
    return Path("data/sentinel")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.training")
    parser.add_argument("--config")
    parser.add_argument("--storage", help="Sentinel storage directory (default: from --config, else data/sentinel)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("collect", help="Test Lab errors and corrections -> example set + COCO export")
    p.add_argument("--out", required=True)
    p.add_argument("--purpose", choices=["evaluation", "training"], default="evaluation")
    p = sub.add_parser("check", help="validate an export")
    p.add_argument("export")
    p = sub.add_parser("plan", help="fine-tuning plan for an export (does not train)")
    p.add_argument("export")
    p = sub.add_parser("register", help="register a candidate ONNX detector")
    p.add_argument("id")
    p.add_argument("path")
    p.add_argument("--family", default="yolox")
    p.add_argument("--labels", default="coco80")
    p.add_argument("--input-size", default="960,544")
    p.add_argument("--score-threshold", type=float, default=0.5)
    p.add_argument("--licence", required=True)
    p.add_argument("--training-data", required=True)
    p.add_argument("--commercial-ok", action="store_true")
    p.add_argument("--base", default="")
    p.add_argument("--notes", default="")
    p = sub.add_parser("compare", help="candidate vs baseline on labelled lab clips")
    p.add_argument("--candidate", required=True)
    p.add_argument("--baseline", default="current")
    p.add_argument("--split", default="test", choices=["test", "validation", "private"])
    p.add_argument("--clips", default="")
    p.add_argument("--fps", type=float, default=5.0)
    p.add_argument("--max-seconds", type=float, default=30.0)
    p.add_argument("--species", action="store_true")
    p.add_argument("--out")
    p = sub.add_parser("promote")
    p.add_argument("id")
    p.add_argument("--by", default="owner")
    p.add_argument("--reason", default="")
    p = sub.add_parser("rollback")
    p.add_argument("--by", default="owner")
    p = sub.add_parser("personalization", help="opt in or out of adapting models with your own footage (local only)")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--by", default="")
    args = parser.parse_args(argv)
    storage = _storage(args)

    from .registry import ModelRegistry

    if args.command == "status":
        from . import status

        print(json.dumps(status(storage), indent=1))
    elif args.command == "collect":
        from .examples import collect

        print(json.dumps(collect(storage, args.out, args.purpose), indent=1))
    elif args.command == "check":
        from .finetune import check

        result = check(args.export)
        print(json.dumps(result, indent=1))
        return 0 if result["ok"] else 1
    elif args.command == "plan":
        from .finetune import plan

        result = plan(args.export)
        print((Path(args.export) / "plan.md").read_text(encoding="utf-8"))
        return 0 if result["ready"] else 2
    elif args.command == "register":
        entry = ModelRegistry(storage).register(
            args.id, args.path, family=args.family, labels=args.labels, input_size=[int(v) for v in args.input_size.split(",")],
            licence=args.licence, training_data=args.training_data, commercial_ok=args.commercial_ok, base=args.base,
            notes=args.notes, score_threshold=args.score_threshold)
        print(json.dumps(entry, indent=1))
    elif args.command == "compare":
        from .evaluate import compare, render

        result = compare(storage, args.candidate, args.baseline, args.split, [c for c in args.clips.split(",") if c],
                         args.fps, args.max_seconds, args.species)
        text = render(result)
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "comparison.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
            (out / "comparison.md").write_text(text, encoding="utf-8")
        print(text)
    elif args.command == "promote":
        print(json.dumps(ModelRegistry(storage).promote(args.id, args.by, reason=args.reason), indent=1))
        print("Put this `detector` block in the site config (Setup editor) to run it live; rollback: python -m sentinel.training rollback")
    elif args.command == "rollback":
        print(json.dumps(ModelRegistry(storage).rollback(args.by), indent=1))
    elif args.command == "personalization":
        from .examples import set_personalization

        print(json.dumps(set_personalization(storage, args.state == "on", args.by), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
