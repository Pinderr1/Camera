from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys

from senior_safety.config_validation import ConfigValidationError


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app
    from .config import load_config
    from .service import SentinelService

    config = load_config(args.config)
    host = args.host or config.get("api", {}).get("host", "127.0.0.1")
    port = args.port or int(config.get("api", {}).get("port", 8080))
    token = os.environ.get("SENTINEL_API_TOKEN", "")
    if not token and not _is_loopback(host):
        raise SystemExit("Refusing to listen on a network interface without SENTINEL_API_TOKEN set.")
    if not token:
        logging.getLogger("sentinel").warning("SENTINEL_API_TOKEN not set: API is open to this machine only (%s).", host)

    service = SentinelService(config, start_cameras=not args.no_cameras)
    service.start()
    try:
        uvicorn.run(create_app(service, api_token=token or None), host=host, port=port, log_level="info")
    finally:
        service.stop()


def validate(args: argparse.Namespace) -> None:
    from .config import load_config

    config = load_config(args.config, resolve_secrets=False)
    print(f"OK: {len(config['cameras'])} camera(s), {sum(len(c.get('zones', [])) for c in config['cameras'])} zone(s), {len(config.get('rules', []))} rule(s).")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="sentinel", description="Sentinel property security brain")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="Run cameras, event engine, API and dashboard")
    serve_parser.add_argument("--config", default="config/sentinel.example.json")
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument("--no-cameras", action="store_true", help="API/dashboard only (review stored history)")
    serve_parser.set_defaults(handler=serve)

    validate_parser = commands.add_parser("validate", help="Validate a site config")
    validate_parser.add_argument("--config", default="config/sentinel.example.json")
    validate_parser.set_defaults(handler=validate)

    commands.add_parser("replay", help="Offline scenario replay (see: python -m sentinel.replay --help)", add_help=False)

    if len(sys.argv) > 1 and sys.argv[1] == "replay":
        from .replay import main as replay_main

        sys.argv = [f"{sys.argv[0]} replay"] + sys.argv[2:]
        replay_main()
        return

    args = parser.parse_args()
    try:
        args.handler(args)
    except ConfigValidationError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
