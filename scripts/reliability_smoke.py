"""Reliability smoke test (about one minute, no cameras needed).

Runs one Sentinel service with five simulated cameras and scheduled faults:
a network disconnect, frozen video, a crashing capture thread, a stalled
decoder, and failing inference; plus a disk-full period, a graceful service
restart, and a damaged event database. It prints a timeline of camera states and
notifications, then a PASS/FAIL line per check, and exits non-zero on failure.

    PYTHONPATH=src python scripts/reliability_smoke.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.service import SentinelService  # noqa: E402
from sentinel.simulation import reset_fault_clocks  # noqa: E402

FULL = [[0, 0], [1, 0], [1, 1], [0, 1]]
Usage = namedtuple("Usage", "total used free")
FAULTS = {
    "cam_network": [{"type": "disconnect", "at_s": 6, "duration_s": 8}],
    "cam_frozen": [{"type": "freeze", "at_s": 6, "duration_s": 8}],
    "cam_crash": [{"type": "crash", "at_s": 6, "duration_s": 0.5}],
    "cam_stall": [{"type": "stall", "at_s": 6, "duration_s": 4}],
    "cam_ai": [{"type": "detector_error", "at_s": 6, "duration_s": 8}],
}
EXPECTED_STATE = {"cam_network": "offline", "cam_frozen": "frozen", "cam_crash": "restarting", "cam_stall": "restarting", "cam_ai": "ai_failed"}
RUN_S = 32


def build_config(directory: Path) -> dict:
    scenario = directory / "scenario.json"
    scenario.write_text(json.dumps({"duration_s": 60, "actors": []}))
    return {
        "site": {"id": "smoke", "name": "Reliability smoke test"},
        "cameras": [
            {"id": camera_id, "name": camera_id, "fps": 5, "source": {"type": "simulated", "scenario": str(scenario), "faults": faults},
             "zones": [{"id": f"{camera_id}_yard", "name": "Yard", "type": "yard", "polygon": FULL}]}
            for camera_id, faults in FAULTS.items()
        ],
        "rules": [],
        "detector": {"type": "scripted", "retry_s": 1},
        "environment": {"enabled": False},
        "health": {"frame_timeout_s": 2, "frozen_after_s": 2, "startup_grace_s": 10, "notify_after_s": 3, "max_notifications_per_hour": 20,
                   "supervisor": {"stall_after_s": 2, "backoff_initial_s": 0.5, "backoff_max_s": 2, "max_stuck_threads": 10}},
        "storage": {"dir": str(directory / "store"), "check_interval_s": 1, "min_free_mb": 1000, "min_free_pct": 1},
        "drone": {"enabled": False},
    }


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    reset_fault_clocks()
    checks: list[tuple[bool, str]] = []
    with tempfile.TemporaryDirectory() as work:
        directory = Path(work)
        config = build_config(directory)
        free = {"mb": 50_000}
        disk = lambda path: Usage(100_000 * 2**20, 0, free["mb"] * 2**20)  # noqa: E731
        service = SentinelService(json.loads(json.dumps(config)), disk_usage=disk)
        notifications = []
        service.runtime.bus.subscribe(lambda topic, payload: notifications.append((time.monotonic(), payload)) if topic == "notification" else None)
        start = time.monotonic()
        service.start()
        print(f"Running {len(FAULTS)} simulated cameras with scheduled faults for {RUN_S} s ...\n")
        seen: dict[str, list[str]] = {camera_id: [] for camera_id in FAULTS}
        last: dict[str, str] = {}
        coverage_states = set()
        paused_seen = False
        printed = 0
        settle_s = 15  # after the faults, allow a loaded machine a little time to settle before the final checks
        while True:
            elapsed = time.monotonic() - start
            settled = service.runtime.health.coverage(service.runtime.camera_status)["state"] == "full" and not service.supervisor.abandoned
            if elapsed >= RUN_S + settle_s or (elapsed >= RUN_S and settled):
                break
            free["mb"] = 200 if 8 <= elapsed < 16 else 50_000  # disk nearly full between 8 s and 16 s
            for camera_id in FAULTS:
                health = service.runtime.camera_status[camera_id].get("health", {})
                state = health.get("state")
                if state and state != last.get(camera_id):
                    print(f"[{elapsed:5.1f}s] {camera_id:12s} {last.get(camera_id, '-'):>11s} -> {state}" + (f"  ({health.get('reason')})" if health.get("reason") else ""))
                    last[camera_id] = state
                    seen[camera_id].append(state)
            coverage_states.add(service.runtime.health.coverage(service.runtime.camera_status)["state"])
            paused_seen = paused_seen or any(recorder.paused for recorder in service.runtime.recorders.values())
            for at, payload in notifications[printed:]:
                print(f"[{at - start:5.1f}s] NOTIFY {payload['severity']:6s} {payload['title']}")
            printed = len(notifications)
            time.sleep(0.2)

        for camera_id, expected in EXPECTED_STATE.items():
            states = seen[camera_id]
            recovered = states and states[-1] == "monitoring" and expected in states
            checks.append((bool(recovered), f"{camera_id}: reported '{expected}' while failing and recovered to monitoring ({' -> '.join(states)})"))
        titles = [payload["title"] for _, payload in notifications]
        checks.append((any("not monitoring" in title for title in titles), "coverage loss was notified"))
        checks.append((any(title.startswith("Monitoring restored") for title in titles), "recovery was notified"))
        checks.append((paused_seen, "recording paused while the disk was nearly full"))
        checks.append((not any(recorder.paused for recorder in service.runtime.recorders.values()), "recording resumed after space was freed"))
        checks.append(("degraded" in coverage_states, "dashboard coverage showed 'degraded' during the faults"))
        checks.append((service.runtime.health.coverage(service.runtime.camera_status)["state"] == "full", "full coverage at the end"))
        outages = service.runtime.store.list_outages(limit=1000)
        checks.append((len([o for o in outages if o["scope"] == "camera"]) >= len(FAULTS), f"{len(outages)} outages recorded in the history"))
        checks.append((not service.supervisor.abandoned, "no stuck threads left behind"))
        incidents = service.runtime.store.list_incidents()
        checks.append((incidents == [], "no security incident was opened by health problems"))
        backup = service.create_backup(label="smoke")
        service.stop()

        # Graceful restart: the gap is recorded, not pushed.
        reset_fault_clocks()
        quiet = json.loads(json.dumps(config))
        for camera in quiet["cameras"]:
            camera["source"].pop("faults")
        restarted = SentinelService(quiet, start_cameras=False)
        restarted.start()
        kinds = [o["kind"] for o in restarted.runtime.store.list_outages(limit=1000) if o["scope"] == "service"]
        checks.append(("service_stopped" in kinds, "graceful restart recorded as a planned stop"))
        restarted.stop()

        # Damaged database: restored from the backup made above.
        database = Path(config["storage"]["dir"]) / "sentinel.db"
        for suffix in ("-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        database.write_bytes(b"damaged" * 1000)
        recovered = SentinelService(quiet, start_cameras=False)
        report = recovered.db_recovery or {}
        checks.append((report.get("action") == "restored_from_backup" and report.get("backup") == backup["name"],
                       f"damaged database restored from {backup['name']}"))
        checks.append((len(recovered.runtime.store.list_outages(limit=1000)) >= len(FAULTS), "outage history survived via the backup"))
        recovered.runtime.close()

    print()
    for ok, text in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {text}")
    failed = sum(1 for ok, _ in checks if not ok)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
