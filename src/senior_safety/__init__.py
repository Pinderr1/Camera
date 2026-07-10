"""Senior night safety prototype core."""

from .schemas import DetectorDecision, NormalizedEvent

__all__ = [
    "DetectorDecision",
    "NightSafetyStateMachine",
    "NormalizedEvent",
    "load_rules",
]


def __getattr__(name: str):
    if name in {"NightSafetyStateMachine", "load_rules"}:
        from .state_machine import NightSafetyStateMachine, load_rules

        return {"NightSafetyStateMachine": NightSafetyStateMachine, "load_rules": load_rules}[name]
    raise AttributeError(name)
