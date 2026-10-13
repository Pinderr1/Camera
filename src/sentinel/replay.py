"""Deterministic offline replay of a scenario through tracker → engine →
incidents → alert policy, in virtual time. Used by tests and for tuning rules
without waiting for real-time playback."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import cameras, load_config
from .engine import ZoneMap
from .models import Notification
from .notify import CallbackNotifier
from .pipeline import filter_detections
from .runtime import SentinelRuntime
from .simulation import Scenario
from .tracker import IoUTracker, TrackerConfig


def run_scenario(
    config: dict[str, Any],
    scenario: Scenario,
    *,
    fps: float = 5.0,
    start: datetime | None = None,
    arm_mode: str | None = None,
    storage_dir: str | Path | None = None,
    tail_s: float = 180.0,
) -> dict[str, Any]:
    notifications: list[Notification] = []
    with tempfile.TemporaryDirectory() as temporary:
        runtime = SentinelRuntime(
            config,
            storage_dir=storage_dir or temporary,
            notifiers=[CallbackNotifier("capture", notifications.append)],
        )
        try:
            if arm_mode:
                runtime.update_settings({"arm_mode": arm_mode}, "replay")
            start_ms = int((start or datetime.now()).timestamp() * 1000)
            runtime.started_ms = start_ms
            zones = ZoneMap(config)
            detector_settings = config.get("detector", {})
            min_area = float(detector_settings.get("min_box_area", 0.0004))
            trackers = {
                camera["id"]: IoUTracker(camera["id"], TrackerConfig.from_dict(config.get("tracker")))
                for camera in cameras(config)
            }
            step_ms = int(1000 / fps)
            total_ms = int((scenario.duration_s + tail_s) * 1000)
            all_events = []
            for offset in range(0, total_ms, step_ms):
                now_ms = start_ms + offset
                t_s = offset / 1000
                for camera_id, tracker in trackers.items():
                    raw = scenario.detections_at(camera_id, t_s) if t_s < scenario.duration_s else []
                    detections = filter_detections(raw, camera_id, zones, min_area, set())
                    active, ended = tracker.update(detections, now_ms)
                    runtime.update_camera_status(camera_id, connected=True, last_frame_ms=now_ms)
                    all_events.extend(runtime.process_camera_update(camera_id, now_ms, active, ended))
                if offset % 1000 < step_ms:
                    runtime.housekeeping(now_ms)
            runtime.dispatcher.stop()
            incidents = runtime.store.list_incidents(limit=500)
            return {
                "scenario": scenario.name,
                "events": [event.as_dict() for event in all_events],
                "incidents": incidents,
                "notifications": [notification.as_dict() for notification in notifications],
                "site_tracks": [site.summary() for site in runtime.engine.site_tracks()],
                "ambiguous_links": runtime.engine.correlator.ambiguous_count,
            }
        finally:
            runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a Sentinel scenario offline and print events/incidents.")
    parser.add_argument("--config", default="config/sentinel.example.json")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--arm-mode", choices=["disarmed", "home", "away"])
    parser.add_argument("--at", help="Virtual start time, ISO format (affects active_hours rules)")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    args = parser.parse_args()

    config = load_config(args.config, resolve_secrets=False)
    for channel in config.get("alerts", {}).get("channels", []):
        channel["enabled"] = False  # replay never pushes to real phones
    result = run_scenario(
        config,
        Scenario.load(args.scenario),
        arm_mode=args.arm_mode,
        start=datetime.fromisoformat(args.at) if args.at else None,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return
    start = result["events"][0]["timestamp_ms"] if result["events"] else 0
    for event in result["events"]:
        print(f"+{(event['timestamp_ms'] - start) / 1000:6.1f}s  {event['severity']:6}  {event['event_type']:17} {event['camera_id']:14} {event['summary']}")
    print(f"\n{len(result['incidents'])} incident(s):")
    for incident in result["incidents"]:
        print(f"  {incident['severity']:6} {incident['title']}  cameras={','.join(incident['cameras'])}  behaviours={','.join(incident['event_types'])}")
    print(f"{len(result['notifications'])} notification(s):")
    for notification in result["notifications"]:
        print(f"  [{notification['severity']}] {notification['title']}")
    print(f"ambiguous cross-camera links refused: {result['ambiguous_links']}")


if __name__ == "__main__":
    main()
