from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from senior_safety.config_validation import ConfigValidationError

log = logging.getLogger("sentinel")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _storage_dir(config: dict) -> Path:
    return Path(config.get("storage", {}).get("dir", "data/sentinel"))


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from . import daemon
    from .api import create_app
    from .config import load_config
    from .service import SentinelService

    config = load_config(args.config, resolve_secrets=False)  # placeholders are resolved by the service, never persisted
    storage_dir = _storage_dir(config)
    daemon.configure_logging(storage_dir, config.get("logging"))
    host = args.host or config.get("api", {}).get("host", "127.0.0.1")
    port = args.port or int(config.get("api", {}).get("port", 8080))
    token = os.environ.get("SENTINEL_API_TOKEN", "")
    if not token and not _is_loopback(host):
        log.error("Refusing to listen on a network interface without SENTINEL_API_TOKEN set.")
        sys.exit(daemon.EXIT_CONFIG)
    if not token:
        log.warning("SENTINEL_API_TOKEN not set: API is open to this machine only (%s).", host)
    if not args.supervised:
        daemon.clear_stop(storage_dir)

    service = SentinelService(config, start_cameras=not args.no_cameras)
    if args.simulate_network:
        from .netdiag import SimulatedNetwork

        service.network = SimulatedNetwork.from_file(args.simulate_network)
        log.warning("Network diagnostics use the simulated network in %s.", args.simulate_network)
    try:
        service.start()
    except RuntimeError as error:
        log.error("%s", error)
        sys.exit(daemon.EXIT_CONFIG)
    server = uvicorn.Server(uvicorn.Config(create_app(service, api_token=token or None), host=host, port=port, log_level="info"))
    exit_code = 0
    finished = threading.Event()

    def fatal(reason: str) -> None:
        nonlocal exit_code
        exit_code = daemon.EXIT_FATAL
        log.critical("exiting so the service manager can start a fresh process: %s", reason)
        server.should_exit = True

    if args.exit_on_fatal or args.supervised:
        service.on_fatal = fatal

    def watch_stop_requests() -> None:
        while not finished.wait(1.0):
            if daemon.stop_requested(storage_dir):
                log.info("stop requested: shutting down gracefully")
                server.should_exit = True
                return

    threading.Thread(target=watch_stop_requests, name="sentinel-stop-watch", daemon=True).start()
    try:
        server.run()  # handles Ctrl+C and SIGTERM as a graceful shutdown
    finally:
        finished.set()
        service.stop()
        if not args.supervised:
            daemon.clear_stop(storage_dir)
        log.info("Sentinel stopped")
    if exit_code:
        sys.exit(exit_code)


def run(args: argparse.Namespace) -> None:
    """Supervised background run: restarts `serve` after crashes (backoff, restart budget)."""
    from . import daemon
    from .config import load_config

    config = load_config(args.config, resolve_secrets=False)
    storage_dir = _storage_dir(config)
    daemon.configure_logging(storage_dir, config.get("logging"), filename="supervisor.log")
    extra = daemon.load_env_file(args.env_file) if args.env_file else {}
    command = [sys.executable, "-m", "sentinel", "serve", "--config", str(Path(args.config).resolve()), "--supervised"]
    for flag, value in (("--host", args.host), ("--port", args.port)):
        if value:
            command += [flag, str(value)]
    supervisor = config.get("health", {}).get("process", {})
    code = daemon.supervise(
        command, storage_dir, env=daemon.child_environment(extra),
        backoff_initial_s=float(supervisor.get("backoff_initial_s", 5)), backoff_max_s=float(supervisor.get("backoff_max_s", 300)),
        max_restarts=int(supervisor.get("max_restarts", 10)), window_s=float(supervisor.get("window_s", 3600)),
        cooldown_s=float(supervisor.get("cooldown_s", 1800)),
    )
    sys.exit(code)


def service_command(args: argparse.Namespace) -> None:
    from . import daemon
    from .config import load_config

    config = load_config(args.config, resolve_secrets=False)
    storage_dir = _storage_dir(config)
    if args.action == "unit":
        print(daemon.systemd_unit(config=args.config, user=os.environ.get("USER", "sentinel"), env_file=args.env_file))
        return
    if args.action == "stop":
        if not daemon.RunState(storage_dir).other_instance():
            print("Sentinel is not running.")
            return
        daemon.request_stop(storage_dir)
        deadline = time.time() + 90
        while time.time() < deadline:
            state = daemon.RunState(storage_dir).read() or {}
            if state.get("clean"):
                print("Sentinel stopped cleanly.")
                return
            time.sleep(1)
        print("Sentinel did not confirm a clean stop within 90 s; check logs in", storage_dir / "logs")
        sys.exit(1)
    if args.action == "status":
        state = daemon.RunState(storage_dir).read()
        running = daemon.RunState(storage_dir).other_instance()
        if state:
            stamp = lambda ms: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms else "-"  # noqa: E731
            print(f"Running: {'yes (pid ' + str(running['pid']) + ')' if running else 'no'}")
            print(f"Started: {stamp(state.get('started_ms'))}  last heartbeat: {stamp(state.get('heartbeat_ms'))}  clean stop: {state.get('clean')}")
        else:
            print("Sentinel has not run with this storage folder yet.")
        if sys.platform == "win32":
            query = subprocess.run(["schtasks", "/Query", "/TN", daemon.TASK_NAME, "/FO", "LIST"], capture_output=True, text=True, check=False)
            print(query.stdout.strip() if query.returncode == 0 else f"Scheduled task '{daemon.TASK_NAME}': not installed (python -m sentinel service install)")
        return
    if sys.platform != "win32":
        raise SystemExit("Background install uses Windows Task Scheduler. On Linux, use: python -m sentinel service unit > /etc/systemd/system/sentinel.service")
    if args.action == "install":
        with tempfile.TemporaryDirectory() as work:
            xml_path = Path(work) / "sentinel-task.xml"
            xml, commands = daemon.install_commands(config=args.config, trigger=args.trigger, user=daemon.current_user(), env_file=args.env_file, xml_path=xml_path)
            if args.dry_run:
                print(xml)
                for command in commands:
                    print(" ".join(command))
                return
            xml_path.write_text(xml, encoding="utf-16")
            for command in commands:
                if args.trigger == "startup":
                    print("Windows will ask for your account password so the task can start before anyone logs in.")
                subprocess.run(command, check=True)
        print(f"Installed scheduled task '{daemon.TASK_NAME}' ({args.trigger}). Start it now with: python -m sentinel service start")
    elif args.action == "uninstall":
        subprocess.run(["schtasks", "/Delete", "/TN", daemon.TASK_NAME, "/F"], check=True)
    elif args.action == "start":
        subprocess.run(["schtasks", "/Run", "/TN", daemon.TASK_NAME], check=True)


def backup_command(args: argparse.Namespace) -> None:
    from . import backup, daemon
    from .config import load_config

    config = load_config(args.config, resolve_secrets=False)
    storage_dir = _storage_dir(config)
    if args.action == "create":
        from .site_config import SiteConfigManager

        effective = SiteConfigManager.load_effective(config, storage_dir)  # the edited site.json, or the start config
        site = {key: value for key, value in effective.items() if not key.startswith("_")}
        info = backup.create_backup(storage_dir, config=site, label=args.label, include_credentials=args.include_credentials)
        print(f"Wrote {backup.backups_dir(storage_dir) / info['name']} ({info['size_bytes'] // 1024} KB): {', '.join(info['files'])}")
    elif args.action == "list":
        for item in backup.list_backups(storage_dir):
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("created_ms", 0) / 1000)) if item.get("valid") else "unreadable"
            print(f"{item['name']}  {created}  {item.get('size_bytes', 0) // 1024} KB  {', '.join(item.get('files', []))}")
    elif args.action == "verify":
        path = Path(args.name) if Path(args.name).is_file() else backup.resolve_backup(storage_dir, args.name)
        result = backup.verify_backup(path)
        print("OK" if result["ok"] else "FAILED:\n- " + "\n- ".join(result["problems"]))
        sys.exit(0 if result["ok"] else 1)
    elif args.action == "restore":
        running = daemon.RunState(storage_dir).other_instance()
        if running:
            raise SystemExit(f"Sentinel is running (pid {running['pid']}). Stop it first (python -m sentinel service stop), "
                             "or restore from the dashboard (System tab), which stages the database for the next start.")
        path = Path(args.name) if Path(args.name).is_file() else backup.resolve_backup(storage_dir, args.name)
        result = backup.restore_backup(path, storage_dir, parts=tuple(args.parts.split(",")))
        print(f"Restored {', '.join(result['restored']) or 'nothing'} from {result['backup']}."
              + (f" The previous state was saved as {result['safety_backup']}." if result["safety_backup"] else ""))


def validate(args: argparse.Namespace) -> None:
    from .config import load_config

    config = load_config(args.config, resolve_secrets=False)
    cameras = config.get("cameras", [])
    print(f"OK: {len(cameras)} camera(s), {sum(len(c.get('zones', [])) for c in cameras)} zone(s), {len(config.get('rules', []))} rule(s), "
          f"{len(config.get('devices', []))} other device(s).")


def main() -> None:
    if sys.stderr is not None:  # pythonw (background task) has no console; logs then go to <storage>/logs only
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser =argparse.ArgumentParser(prog="sentinel", description="Sentinel property security brain")
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="Run cameras, event engine, API and dashboard")
    serve_parser.add_argument("--config", default="config/sentinel.example.json")
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument("--no-cameras", action="store_true", help="API/dashboard only (review stored history)")
    serve_parser.add_argument("--simulate-network", metavar="SCENARIO", help="Answer Setup network diagnostics from a scenario (config/netsim/*.json)")
    serve_parser.add_argument("--exit-on-fatal", action="store_true", help="Exit with code 3 when processing threads are stuck (for systemd/service managers)")
    serve_parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    serve_parser.set_defaults(handler=serve)

    run_parser = commands.add_parser("run", help="Serve under a supervisor that restarts after crashes (background use)")
    run_parser.add_argument("--config", default="config/sentinel.example.json")
    run_parser.add_argument("--env-file", help="KEY=VALUE file with secrets such as SENTINEL_API_TOKEN")
    run_parser.add_argument("--host")
    run_parser.add_argument("--port", type=int)
    run_parser.set_defaults(handler=run)

    service_parser = commands.add_parser("service", help="Background start-up: install, uninstall, start, stop, status, unit")
    service_parser.add_argument("action", choices=["install", "uninstall", "start", "stop", "status", "unit"])
    service_parser.add_argument("--config", default="config/sentinel.example.json")
    service_parser.add_argument("--trigger", choices=["startup", "logon"], default="startup", help="Windows: start at boot (asks for your password) or at logon")
    service_parser.add_argument("--env-file")
    service_parser.add_argument("--dry-run", action="store_true", help="Print the task definition instead of installing it")
    service_parser.set_defaults(handler=service_command)

    backup_parser = commands.add_parser("backup", help="Backups of config, settings and the event database")
    backup_parser.add_argument("action", choices=["create", "list", "verify", "restore"])
    backup_parser.add_argument("name", nargs="?", help="Backup file name (verify/restore)")
    backup_parser.add_argument("--config", default="config/sentinel.example.json")
    backup_parser.add_argument("--label", default="manual")
    backup_parser.add_argument("--include-credentials", action="store_true", help="Include the encrypted camera passwords (same Windows account only)")
    backup_parser.add_argument("--parts", default="config,settings,database,assets")
    backup_parser.set_defaults(handler=backup_command)

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
    if args.command == "backup" and args.action in ("verify", "restore") and not args.name:
        parser.error(f"backup {args.action} needs a backup name")
    try:
        args.handler(args)
    except ConfigValidationError as error:
        print(str(error), file=sys.stderr)
        sys.exit(78)


if __name__ == "__main__":
    main()
