from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    sensor_id: str
    sensor_type: str
    room: str
    zone_id: str
    timestamp_ms: int
    event_name: str
    value: Any
    confidence: float = 1.0
    battery_ok: bool = True
    network_ok: bool = True
    event_time_local: str = ""
    notes: str = ""


@dataclass(frozen=True)
class DetectorDecision:
    timestamp_ms: int
    state: str
    severity: str
    score: float
    confidence: float
    reason_codes: list[str] = field(default_factory=list)
    recommended_action: str = "observe"
    suppressions: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "state": self.state,
            "severity": self.severity,
            "score": self.score,
            "confidence": self.confidence,
            "reason_codes": self.reason_codes,
            "recommended_action": self.recommended_action,
            "suppressions": self.suppressions,
            "debug": self.debug,
        }
