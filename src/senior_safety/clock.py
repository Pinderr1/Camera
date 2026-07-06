from __future__ import annotations

from datetime import datetime, time
from typing import Any, Iterable

from .schemas import NormalizedEvent

TICK_EVENT_NAME = "tick"
DEFAULT_TICK_INTERVAL_S = 15


def tick_interval_s(rules: dict[str, Any]) -> int:
    return int(rules.get("runtime", {}).get("tick_interval_s", DEFAULT_TICK_INTERVAL_S))


def make_tick(timestamp_ms: int, event_time_local: str = "") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"tick_{timestamp_ms}",
        sensor_id="clock",
        sensor_type="clock",
        room="",
        zone_id="",
        timestamp_ms=timestamp_ms,
        event_time_local=event_time_local,
        event_name=TICK_EVENT_NAME,
        value=True,
    )


def synthesize_ticks(events: Iterable[NormalizedEvent], interval_ms: int) -> list[NormalizedEvent]:
    """Interleave tick events between timestamp-ordered sensor events so
    duration-based rules fire during replay even when sensors are quiet."""
    result: list[NormalizedEvent] = []
    previous_ms: int | None = None
    for event in events:
        if previous_ms is not None:
            tick_ms = previous_ms + interval_ms
            while tick_ms < event.timestamp_ms:
                result.append(make_tick(tick_ms))
                tick_ms += interval_ms
        result.append(event)
        previous_ms = event.timestamp_ms
    return result


def parse_local_datetime(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def is_night_window(rules: dict[str, Any], local_dt: datetime) -> bool:
    window = rules.get("night_window")
    if not window:
        return True
    start = _parse_hhmm(window["start_local"])
    end = _parse_hhmm(window["end_local"])
    now = local_dt.time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end
