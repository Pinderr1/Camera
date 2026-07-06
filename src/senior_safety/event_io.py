from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from .schemas import DetectorDecision, NormalizedEvent


TRUE_VALUES = {"true", "1", "yes", "on", "open", "occupied", "present"}
FALSE_VALUES = {"false", "0", "no", "off", "closed", "clear", "empty", ""}


def parse_boolish(value: object) -> object:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    try:
        return float(text)
    except ValueError:
        return value


def read_sensor_events(path: str | Path) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            if not row.get("event_name"):
                continue
            timestamp_raw = row.get("event_time_ms") or row.get("timestamp_ms") or index
            event = NormalizedEvent(
                event_id=row.get("event_id") or f"evt_{index}",
                sensor_id=row.get("sensor_id") or "unknown",
                sensor_type=row.get("sensor_type") or "unknown",
                room=row.get("room") or "",
                zone_id=row.get("zone_id") or "",
                timestamp_ms=int(float(timestamp_raw)),
                event_time_local=row.get("event_time_local") or "",
                event_name=row["event_name"].strip(),
                value=parse_boolish(row.get("value")),
                confidence=float(row.get("confidence") or 1.0),
                battery_ok=bool(parse_boolish(row.get("battery_ok", "true"))),
                network_ok=bool(parse_boolish(row.get("network_ok", "true"))),
                notes=row.get("notes") or "",
            )
            events.append(event)
    events.sort(key=lambda event: event.timestamp_ms)
    return events


DECISION_FIELDNAMES = [
    "timestamp_ms",
    "state",
    "severity",
    "score",
    "confidence",
    "reason_codes",
    "recommended_action",
    "suppressions",
    "debug",
]


def _decision_row(decision: DetectorDecision) -> dict[str, object]:
    row = decision.as_dict()
    row["reason_codes"] = "|".join(decision.reason_codes)
    row["suppressions"] = "|".join(decision.suppressions)
    row["debug"] = json.dumps(decision.debug, sort_keys=True)
    return row


def write_decisions_csv(path: str | Path, decisions: Iterable[DetectorDecision]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_FIELDNAMES)
        writer.writeheader()
        for decision in decisions:
            writer.writerow(_decision_row(decision))


def append_csv_row(path: str | Path, fieldnames: list[str], row: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_header = not target.exists() or target.stat().st_size == 0
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def append_decision_csv(path: str | Path, decision: DetectorDecision) -> None:
    append_csv_row(path, DECISION_FIELDNAMES, _decision_row(decision))
