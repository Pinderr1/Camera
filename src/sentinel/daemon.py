"""Running Sentinel unattended: run-state tracking, log rotation, graceful
stop requests, a restarting process supervisor, and background start-up on
Windows through Task Scheduler.

Process layout under `sentinel run`:

    supervisor (this module, tiny, stdlib)  -> restarts on crash with backoff
      └─ child: python -m sentinel serve     -> cameras, engine, API, dashboard

Graceful stop: `sentinel service stop` (or the dashboard's owner) writes
`<storage>/control/stop`. The serving process notices within a second, stops
the camera workers, flushes recordings and the database, and exits 0; the
supervisor then exits too. A clean exit is recorded in `runstate.json`, so the
next start can tell a planned stop from a crash and report the gap in coverage.

Windows background start uses a Task Scheduler task running as the installing
user: camera passwords are DPAPI-encrypted to that account, so a service under
LocalSystem could not read them. No third-party service wrapper is needed."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

log = logging.getLogger("sentinel.daemon")
EXIT_CONFIG = 78  # configuration problem: restarting will not help
EXIT_FATAL = 3  # stuck threads or similar: a fresh process is needed
TASK_NAME = "Sentinel"
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---- run state ---------------------------------------------------------------


class RunState:
    """`<storage>/runstate.json`: pid, start time, a heartbeat every few seconds,
    and whether the last stop was clean."""

    def __init__(self, storage_dir: str | Path) -> None:
        self.path = Path(storage_dir) / "runstate.json"
        self.state: dict[str, Any] = {}

    def read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def other_instance(self, stale_after_s: float = 30.0) -> dict[str, Any] | None:
        previous = self.read()
        if not previous or previous.get("clean") or previous.get("pid") == os.getpid():
            return None
        fresh = time.time() * 1000 - previous.get("heartbeat_ms", 0) < stale_after_s * 1000
        return previous if fresh and pid_alive(int(previous.get("pid", 0))) else None

    def begin(self) -> dict[str, Any] | None:
        """Returns the previous run's state (None on first start)."""
        previous = self.read()
        now = int(time.time() * 1000)
        self.state = {"pid": os.getpid(), "started_ms": now, "heartbeat_ms": now, "clean": False}
        self._write()
        return previous

    def beat(self) -> None:
        if self.state:
            self.state["heartbeat_ms"] = int(time.time() * 1000)
            self._write()

    def end(self) -> None:
        if self.state:
            now = int(time.time() * 1000)
            self.state.update(heartbeat_ms=now, stopped_ms=now, clean=True)
            self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as error:  # a full disk must not stop monitoring
            log.warning("could not write %s: %s", self.path.name, error)


def downtime(previous: dict[str, Any] | None, now_ms: int) -> dict[str, Any] | None:
    """The gap before this start: (start, end, clean) or None on first start."""
    if not previous:
        return None
    if previous.get("clean"):
        start = int(previous.get("stopped_ms") or previous.get("heartbeat_ms") or now_ms)
    else:
        start = int(previous.get("heartbeat_ms") or previous.get("started_ms") or now_ms)
    return {"started_ms": start, "ended_ms": now_ms, "clean": bool(previous.get("clean"))}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- stop requests -------------------------------------------------------------


def _stop_file(storage_dir: str | Path) -> Path:
    return Path(storage_dir) / "control" / "stop"


def request_stop(storage_dir: str | Path) -> None:
    path = _stop_file(storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time() * 1000)), encoding="utf-8")


def stop_requested(storage_dir: str | Path) -> bool:
    return _stop_file(storage_dir).exists()


def clear_stop(storage_dir: str | Path) -> None:
    _stop_file(storage_dir).unlink(missing_ok=True)


# ---- logging -------------------------------------------------------------------


def configure_logging(storage_dir: str | Path, settings: dict[str, Any] | None = None, *, filename: str = "sentinel.log") -> Path:
    """Console (when there is one) plus a size-rotated file in `<storage>/logs/`."""
    settings = settings or {}
    logs = Path(storage_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / filename
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(settings.get("level", "INFO")).upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not any(isinstance(handler, logging.handlers.RotatingFileHandler) and Path(handler.baseFilename) == path.resolve() for handler in root.handlers):
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=int(float(settings.get("max_mb", 10)) * 2**20), backupCount=int(settings.get("backups", 5)), encoding="utf-8", delay=True
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if sys.stderr is not None and not any(type(handler) is logging.StreamHandler for handler in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
    return path


def load_env_file(path: str | Path) -> dict[str, str]:
    """KEY=VALUE lines (# comments). Used to hand secrets such as
    SENTINEL_API_TOKEN to a background process; keep the file outside the repo."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# ---- process supervisor ----------------------------------------------------------


def supervise(
    command: list[str],
    storage_dir: str | Path,
    *,
    env: dict[str, str] | None = None,
    backoff_initial_s: float = 5.0,
    backoff_max_s: float = 300.0,
    max_restarts: int = 10,
    window_s: float = 3600.0,
    cooldown_s: float = 1800.0,
    stop_timeout_s: float = 45.0,
    poll_s: float = 0.5,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> int:
    """Run `command` and restart it when it crashes: exponential backoff, at most
    `max_restarts` per `window_s`, then one trial per `cooldown_s` (no endless
    tight loop). A clean exit (0), a stop request, or a configuration error
    (exit 78) ends supervision."""
    storage_dir = Path(storage_dir)
    clear_stop(storage_dir)
    restarts: list[float] = []
    emit = on_event or (lambda kind, data: None)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" and sys.stderr is None else 0
    while True:
        started = time.monotonic()
        child = subprocess.Popen(command, env=env, creationflags=flags)
        emit("started", {"pid": child.pid})
        log.info("started Sentinel (pid %d)", child.pid)
        code = _wait(child, storage_dir, stop_timeout_s, poll_s)
        if stop_requested(storage_dir):
            clear_stop(storage_dir)
            emit("stopped", {"code": code})
            log.info("Sentinel stopped on request (exit %s)", code)
            return 0
        if code == 0:
            emit("exited", {"code": 0})
            return 0
        if code == EXIT_CONFIG:
            emit("config_error", {"code": code})
            log.error("Sentinel exited with a configuration error; not restarting. Fix the config and start it again.")
            return code
        now = time.monotonic()
        restarts = [at for at in restarts if now - at <= window_s] + [now]
        if len(restarts) > max_restarts:
            delay = cooldown_s
            log.error("Sentinel crashed (exit %s), %d restarts within %.0f min; next attempt in %.0f min",
                      code, len(restarts) - 1, window_s / 60, delay / 60)
        else:
            delay = min(backoff_initial_s * 2 ** (len(restarts) - 1), backoff_max_s)
            log.error("Sentinel crashed (exit %s) after %.0f s; restarting in %g s", code, now - started, delay)
        emit("crashed", {"code": code, "restart_in_s": delay})
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if stop_requested(storage_dir):
                clear_stop(storage_dir)
                return 0
            time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))


def _wait(child: subprocess.Popen, storage_dir: Path, stop_timeout_s: float, poll_s: float) -> int:
    while True:
        code = child.poll()
        if code is not None:
            return code
        if stop_requested(storage_dir):
            try:  # the child watches the same stop file and shuts down on its own
                return child.wait(timeout=stop_timeout_s)
            except subprocess.TimeoutExpired:
                log.error("Sentinel did not stop within %.0f s; terminating it", stop_timeout_s)
                child.terminate()
                try:
                    return child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    return child.wait()
        time.sleep(poll_s)


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ) | (extra or {})
    source = str(REPO_ROOT / "src")
    paths = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join([source] + [part for part in paths if part != source])
    return env


# ---- Windows Task Scheduler -------------------------------------------------------


def task_xml(*, python: str, arguments: str, workdir: str, user: str, trigger: str = "startup") -> str:
    """Task definition: start at boot (or at logon), never time out, keep running
    on battery, one instance only, and restart if launching fails. Crash
    restarts are handled by `sentinel run` itself."""
    if trigger not in ("startup", "logon"):
        raise ValueError("trigger must be startup or logon")
    triggers = (
        "<BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger>" if trigger == "startup"
        else f"<LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>"
    )
    logon_type = "Password" if trigger == "startup" else "InteractiveToken"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Sentinel property security monitoring (local cameras and dashboard).</Description></RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>{logon_type}</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(python)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def install_commands(*, config: str, trigger: str, user: str, env_file: str | None, xml_path: Path, python: str | None = None) -> tuple[str, list[list[str]]]:
    """(task XML, schtasks commands). Kept separate from execution for testing."""
    python = python or _windowless_python()
    launcher = REPO_ROOT / "scripts" / "sentinel_launcher.py"
    arguments = f'"{launcher}" run --config "{Path(config).resolve()}"' + (f' --env-file "{Path(env_file).resolve()}"' if env_file else "")
    xml = task_xml(python=python, arguments=arguments, workdir=str(REPO_ROOT), user=user, trigger=trigger)
    create = ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]
    if trigger == "startup":
        create += ["/RU", user, "/RP", "*"]  # schtasks prompts for the password; it is never passed on a command line
    return xml, [create]


def _windowless_python() -> str:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else Path(sys.executable))


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME") or os.environ.get("USER", "")
    return f"{domain}\\{user}" if domain else user


def systemd_unit(*, config: str, user: str, env_file: str | None) -> str:
    environment = f"EnvironmentFile={Path(env_file).resolve()}\n" if env_file else ""
    return f"""[Unit]
Description=Sentinel property security monitoring
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=3600
StartLimitBurst=10

[Service]
User={user}
WorkingDirectory={REPO_ROOT}
Environment=PYTHONPATH={REPO_ROOT / 'src'}
{environment}ExecStart={sys.executable} -m sentinel serve --config {Path(config).resolve()} --exit-on-fatal
Restart=on-failure
RestartSec=5
RestartPreventExitStatus={EXIT_CONFIG}
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""
