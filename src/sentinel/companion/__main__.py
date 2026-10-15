"""python -m sentinel.companion pair|run|provision"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from pathlib import Path

from .agent import CompanionAgent, load_identity, pair
from .provision import write_bundle
from .relay import MavlinkRelay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel.companion", description="Sentinel drone companion computer")
    commands = parser.add_subparsers(dest="command", required=True)
    pair_cmd = commands.add_parser("pair", help="pair with a Sentinel Hub using the code shown on its Drone tab")
    pair_cmd.add_argument("--hub", required=True, help="Hub address, e.g. http://192.168.1.10:8080")
    pair_cmd.add_argument("--code", required=True)
    pair_cmd.add_argument("--name", default="Sentinel companion")
    pair_cmd.add_argument("--identity", default="/var/lib/sentinel/identity.json")
    run_cmd = commands.add_parser("run", help="relay MAVLink and report health")
    run_cmd.add_argument("--config", required=True, help="companion.json")
    prov = commands.add_parser("provision", help="write companion.json, mediamtx.yml and a systemd unit")
    prov.add_argument("--out", required=True)
    prov.add_argument("--fc", default="tcp:127.0.0.1:5760", help="flight controller endpoint (serial needs mavlink-router)")
    prov.add_argument("--hub-host", required=True)
    prov.add_argument("--gcs", action="append", default=[], help="pilot ground station as udp:IP:PORT (repeatable); none by default")
    prov.add_argument("--camera", choices=["rpi", "rtsp"], default="rpi")
    prov.add_argument("--upstream-rtsp", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "pair":
        result = pair(args.hub, args.code, args.name, Path(args.identity))
        print(json.dumps(result))
        return 0
    if args.command == "provision":
        print(json.dumps(write_bundle(args.out, fc=args.fc, hub_host=args.hub_host, gcs=args.gcs, camera=args.camera,
                                      upstream_rtsp=args.upstream_rtsp), indent=2))
        return 0

    config = json.loads(Path(args.config).read_text())
    relay = MavlinkRelay(config["fc"], config["hub"], config.get("gcs", []), policy=config.get("policy", "telemetry")).start()
    agent = CompanionAgent(load_identity(Path(config["identity"])), relay_status=relay.status, video_url=config.get("video_url", "")).start()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    agent.stop()
    relay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
