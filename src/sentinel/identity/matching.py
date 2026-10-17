"""Turning face comparisons into a cautious known / unknown / unverified status.
Standard library only.

One face comparison never decides anything. Each camera track collects
observations (a match to one member, a clear non-match, an in-between score,
or no usable face), and a status is derived from all of them:

- known: at least `min_agreeing` matches to one enrolled member, clearly more
  than any other member and at least as many as clear non-matches;
- unknown: at least `min_unknown` clear, good-quality faces that matched nobody,
  and few matches; this means "not an enrolled household member", nothing more;
- unverified: everything else (no usable face, outside a recognition zone,
  in-between scores, conflicting matches, recognition off or no members).

Embeddings never reach this module: the face worker compares and discards them."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .settings import IdentitySettings

OUTCOMES = ("match", "no_match", "uncertain", "no_face", "poor_face", "several_faces")


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("empty face embedding")
    return [value / norm for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two L2-normalised vectors."""
    return sum(x * y for x, y in zip(a, b))


@dataclass(frozen=True)
class FaceObservation:
    outcome: str
    member_id: str = ""
    score: float = 0.0  # best similarity to any enrolled member (-1 when nothing was compared)
    margin: float = 0.0  # best minus second-best member
    face_px: int = 0
    ms: int = 0


def judge_scores(scores: dict[str, float], settings: IdentitySettings, face_px: int = 0, ms: int = 0) -> FaceObservation:
    """One good-quality face compared with every enrolled member (member -> best similarity)."""
    if not scores:
        return FaceObservation("no_match", score=-1.0, face_px=face_px, ms=ms)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_id, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    margin = best - second
    if best >= settings.match_threshold and margin >= settings.min_margin:
        return FaceObservation("match", best_id, round(best, 4), round(margin, 4), face_px, ms)
    if best < settings.unknown_below:
        return FaceObservation("no_match", "", round(best, 4), round(margin, 4), face_px, ms)
    return FaceObservation("uncertain", "", round(best, 4), round(margin, 4), face_px, ms)


@dataclass
class Evidence:
    """Counts of face observations for one subject (a camera track, or several linked ones)."""

    matches: dict[str, int] = field(default_factory=dict)
    no_match: int = 0
    uncertain: int = 0
    no_face: int = 0
    attempts: int = 0
    best_scores: dict[str, float] = field(default_factory=dict)

    def add(self, observation: FaceObservation) -> None:
        self.attempts += 1
        if observation.outcome == "match":
            self.matches[observation.member_id] = self.matches.get(observation.member_id, 0) + 1
            self.best_scores[observation.member_id] = max(self.best_scores.get(observation.member_id, -1.0), observation.score)
        elif observation.outcome == "no_match":
            self.no_match += 1
        elif observation.outcome == "uncertain":
            self.uncertain += 1
        else:
            self.no_face += 1

    def merged(self, other: "Evidence") -> "Evidence":
        out = Evidence(dict(self.matches), self.no_match + other.no_match, self.uncertain + other.uncertain,
                       self.no_face + other.no_face, self.attempts + other.attempts, dict(self.best_scores))
        for member, count in other.matches.items():
            out.matches[member] = out.matches.get(member, 0) + count
            out.best_scores[member] = max(out.best_scores.get(member, -1.0), other.best_scores.get(member, -1.0))
        return out

    def forget(self, member_id: str) -> None:
        """A member was removed (consent withdrawn): their matches no longer count."""
        self.matches.pop(member_id, None)
        self.best_scores.pop(member_id, None)

    def faces_compared(self) -> int:
        return sum(self.matches.values()) + self.no_match + self.uncertain

    def summary(self) -> dict[str, Any]:
        return {"faces_compared": self.faces_compared(), "matches": sum(self.matches.values()), "no_match": self.no_match,
                "uncertain": self.uncertain, "no_usable_face": self.no_face}


def decide(evidence: Evidence, settings: IdentitySettings) -> tuple[str, str, str]:
    """(status, member_id, reason)."""
    ranked = sorted(evidence.matches.items(), key=lambda item: item[1], reverse=True)
    total_matches = sum(evidence.matches.values())
    if ranked:
        leader, count = ranked[0]
        others = total_matches - count
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if runner_up >= settings.min_agreeing:
            return "unverified", "", "conflicting_matches"
        if count >= settings.min_agreeing and count >= 3 * others and count >= evidence.no_match:
            return "known", leader, "consistent_matches"
    if evidence.no_match >= settings.min_unknown and 3 * total_matches <= evidence.no_match:
        return "unknown", "", "no_enrolled_member_matched"
    if evidence.faces_compared() == 0:
        return "unverified", "", "no_usable_face" if evidence.attempts else "not_checked"
    return "unverified", "", "insufficient_evidence"


def combine(evidences: Iterable[Evidence]) -> Evidence:
    total = Evidence()
    for evidence in evidences:
        total = total.merged(evidence)
    return total
