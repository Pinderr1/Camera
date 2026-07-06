from __future__ import annotations

import csv
from pathlib import Path

from .schemas import NormalizedEvent


OMNIFALL_TO_EVENT = {
    "fall": ("fall_suspected", True),
    "falling": ("fall_suspected", True),
    "fallen": ("floor_level_posture", True),
    "lying": ("floor_level_posture", True),
    "stand_up": ("floor_level_posture", False),
    "walk": ("route_motion", True),
    "walking": ("route_motion", True),
    "sit_down": ("person_present", True),
    "lie_down": ("floor_level_posture", True),
    "no_motion": ("no_motion", True),
}


def read_omnifall_segments_csv(path: str | Path) -> list[NormalizedEvent]:
    """Read a lightweight OmniFall segment export.

    Expected columns are intentionally generic so we can use Hugging Face exports
    without duplicating upstream ingestion logic:

    `clip_id,label,start_ms,end_ms,split,dataset`
    """

    events: list[NormalizedEvent] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            label = (row.get("label") or row.get("state") or "").strip().lower()
            if label not in OMNIFALL_TO_EVENT:
                continue
            event_name, value = OMNIFALL_TO_EVENT[label]
            start_ms = int(float(row.get("start_ms") or row.get("timestamp_ms") or index))
            events.append(
                NormalizedEvent(
                    event_id=f"omnifall_{index}",
                    sensor_id="omnifall_replay",
                    sensor_type="public_dataset_replay",
                    room="public_dataset",
                    zone_id="public_dataset",
                    timestamp_ms=start_ms,
                    event_name=event_name,
                    value=value,
                    confidence=1.0,
                    notes=f"clip_id={row.get('clip_id', '')}; label={label}",
                )
            )
            if label in {"fallen", "lying"}:
                end_ms = int(float(row.get("end_ms") or start_ms))
                if end_ms > start_ms:
                    events.append(
                        NormalizedEvent(
                            event_id=f"omnifall_{index}_still",
                            sensor_id="omnifall_replay",
                            sensor_type="public_dataset_replay",
                            room="public_dataset",
                            zone_id="public_dataset",
                            timestamp_ms=end_ms,
                            event_name="no_motion",
                            value=True,
                            confidence=0.8,
                            notes=f"clip_id={row.get('clip_id', '')}; generated stillness marker",
                        )
                    )
    events.sort(key=lambda event: event.timestamp_ms)
    return events
