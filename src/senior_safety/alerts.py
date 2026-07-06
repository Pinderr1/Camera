from __future__ import annotations

from dataclasses import dataclass

from .schemas import DetectorDecision


@dataclass(frozen=True)
class AlertPayload:
    title: str
    message: str
    severity: str
    state: str
    reason_codes: list[str]
    recommended_action: str
    acknowledge_required: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "state": self.state,
            "reason_codes": self.reason_codes,
            "recommended_action": self.recommended_action,
            "acknowledge_required": self.acknowledge_required,
        }


def build_alert_payload(decision: DetectorDecision) -> AlertPayload | None:
    if decision.severity not in {"low", "urgent"}:
        return None

    title = "Urgent senior night check" if decision.severity == "urgent" else "Senior night check"
    duration_bits = []
    for key in ("bed_unoccupied_s", "bathroom_occupied_s", "no_motion_s"):
        value = decision.debug.get(key, 0)
        if isinstance(value, (int, float)) and value > 0:
            duration_bits.append(f"{key}={int(value)}s")
    duration_text = ", ".join(duration_bits) if duration_bits else "duration not available"
    reasons = ", ".join(decision.reason_codes) if decision.reason_codes else "no reason code"
    message = f"State: {decision.state}. Reasons: {reasons}. Durations: {duration_text}."

    return AlertPayload(
        title=title,
        message=message,
        severity=decision.severity,
        state=decision.state,
        reason_codes=decision.reason_codes,
        recommended_action=decision.recommended_action,
        acknowledge_required=decision.severity == "urgent",
    )
