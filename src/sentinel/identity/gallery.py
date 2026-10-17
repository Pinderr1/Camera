"""Enrolled household members and their consent records. Standard library only.

What is stored, per member: a display name, the consent record (who agreed,
for whom, how, when, and which statement), and face *templates*: 128 numbers
per enrollment photo computed by the recognizer. Photos are never stored. The
file is encrypted for the service's Windows account with DPAPI (elsewhere it
is a 0600 file, like the credential store), so a copied file is useless.

Withdrawing consent deletes the member and every template at once; nothing
biometric is kept for "history". The audit log records who enrolled or removed
whom and when, never a template."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from ..credentials import protect_bytes, unprotect_bytes
from .matching import cosine, normalize
from .settings import IdentitySettings

CONSENT_STATEMENT_VERSION = "2026-10-16"
CONSENT_STATEMENT = (
    "I agree that this Sentinel system may keep face templates of me (numbers computed from photos; the photos themselves are not kept) "
    "on this computer, and compare faces that its cameras see inside the configured recognition zones with them, so that it can tell "
    "household members apart from people it does not recognize. The templates stay on this computer and are never sent to another "
    "service. I can withdraw this consent at any time, and my templates are then deleted immediately. Recognition can be wrong, and "
    "Sentinel never treats a person it does not recognize as a threat."
)
RELATIONSHIPS = ("self", "parent_or_guardian")  # an adult agrees for themselves; a parent or guardian for a child
METHODS = ("in_person", "written", "digital")
EMBEDDING_SIZE = 128


class EnrollmentError(ValueError):
    """An enrollment was refused; the message says why in plain language."""


def validate_consent(consent: dict[str, Any] | None) -> dict[str, Any]:
    consent = consent or {}
    problems = []
    if consent.get("agreed") is not True:
        problems.append("the person (or their parent or guardian) must have agreed to the consent statement")
    given_by = str(consent.get("given_by", "")).strip()
    if not 0 < len(given_by) <= 80:
        problems.append("consent.given_by must name who agreed (1-80 characters)")
    if consent.get("relationship") not in RELATIONSHIPS:
        problems.append(f"consent.relationship must be one of {list(RELATIONSHIPS)}")
    if consent.get("method") not in METHODS:
        problems.append(f"consent.method must be one of {list(METHODS)}")
    if consent.get("statement_version") != CONSENT_STATEMENT_VERSION:
        problems.append(f"consent.statement_version must be {CONSENT_STATEMENT_VERSION!r} (show the person the current statement)")
    if problems:
        raise EnrollmentError("; ".join(problems))
    return {"agreed": True, "given_by": given_by, "relationship": consent["relationship"], "method": consent["method"],
            "statement_version": CONSENT_STATEMENT_VERSION, "note": str(consent.get("note", "")).strip()[:200]}


class FaceGallery:
    def __init__(self, path: str | Path, *, protect: Callable[[bytes], bytes] = protect_bytes,
                 unprotect: Callable[[bytes], bytes] = unprotect_bytes) -> None:
        self.path = Path(path)
        self._protect, self._unprotect = protect, unprotect
        self._lock = threading.RLock()
        self._members: dict[str, dict[str, Any]] = {}
        self._mtime: float | None = None
        self._loaded = False
        self.version = 0  # bumps on every change seen by this process (the face worker reloads on it)
        self.error = ""
        self._load()

    # ---- reading --------------------------------------------------------------

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            mtime = None
        if self._loaded and mtime == self._mtime:
            return
        members: dict[str, dict[str, Any]] = {}
        if mtime is not None:
            try:
                data = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
                members = {member["member_id"]: member for member in data.get("members", [])}
                self.error = ""
            except Exception as error:  # wrong account, damaged file: recognition stays off, enrolled data is not overwritten
                self.error = f"gallery unreadable ({type(error).__name__}); recognition paused"
                members = {}
        self._members, self._mtime, self._loaded = members, mtime, True
        self.version += 1

    def refresh(self) -> int:
        """Pick up changes written by another process (the `sentinel.identity` command)."""
        with self._lock:
            self._load()
            return self.version

    def members(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load()
            return [self._public(member) for member in sorted(self._members.values(), key=lambda m: m["enrolled_ms"])]

    def get(self, member_id: str) -> dict[str, Any]:
        with self._lock:
            self._load()
            member = self._members.get(member_id)
            if member is None:
                raise KeyError(member_id)
            return self._public(member)

    def names(self) -> dict[str, str]:
        with self._lock:
            self._load()
            return {member_id: member["display_name"] for member_id, member in self._members.items()}

    def templates(self, settings: IdentitySettings) -> dict[str, list[list[float]]]:
        """Templates of members that finished enrollment (enough photos). Only the face worker reads these."""
        with self._lock:
            self._load()
            return {member_id: [list(t) for t in member["templates"]] for member_id, member in self._members.items()
                    if len(member["templates"]) >= settings.enroll_min_photos}

    @staticmethod
    def _public(member: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in member.items() if key != "templates"} | {"photos_enrolled": len(member["templates"])}

    # ---- changes --------------------------------------------------------------

    def add_member(self, display_name: str, consent: dict[str, Any] | None, actor: str, now_ms: int) -> dict[str, Any]:
        display_name = str(display_name or "").strip()
        if not 0 < len(display_name) <= 60:
            raise EnrollmentError("display_name must be 1-60 characters")
        record = validate_consent(consent) | {"recorded_by": actor[:64], "recorded_ms": now_ms}
        with self._lock:
            self._load()
            if self.error:
                raise EnrollmentError(self.error)
            if any(member["display_name"].lower() == display_name.lower() for member in self._members.values()):
                raise EnrollmentError(f"a member called {display_name!r} is already enrolled")
            member_id = f"m_{secrets.token_hex(4)}"
            self._members[member_id] = {"member_id": member_id, "display_name": display_name, "role": "household",
                                        "consent": record, "enrolled_ms": now_ms, "updated_ms": now_ms, "templates": []}
            self._save()
            return self._public(self._members[member_id])

    def add_templates(self, member_id: str, templates: Sequence[Sequence[float]], settings: IdentitySettings, now_ms: int) -> dict[str, Any]:
        """Add face templates from enrollment photos, after checking they show one
        consistent person who is not already enrolled as someone else."""
        cleaned = []
        for template in templates:
            if len(template) != EMBEDDING_SIZE:
                raise EnrollmentError(f"face template must have {EMBEDDING_SIZE} values")
            cleaned.append([round(value, 6) for value in normalize(template)])
        if not cleaned:
            raise EnrollmentError("no face templates to add")
        with self._lock:
            self._load()
            member = self._members.get(member_id)
            if member is None:
                raise KeyError(member_id)
            combined = member["templates"] + cleaned
            if len(combined) > settings.max_templates:
                raise EnrollmentError(f"at most {settings.max_templates} photos per member; remove the member and enroll again to start over")
            weakest = consistency(combined)
            if weakest is not None and weakest < settings.match_threshold:
                raise EnrollmentError("the photos do not look like one person (or are too poor to compare): "
                                      f"lowest agreement {weakest:.2f}, needed {settings.match_threshold:.2f}. Nothing was added.")
            for other_id, other in self._members.items():
                if other_id == member_id or not other["templates"]:
                    continue
                similarity = max(cosine(a, b) for a in cleaned for b in other["templates"])
                if similarity >= settings.match_threshold:
                    raise EnrollmentError(f"these photos match {other['display_name']}, who is already enrolled "
                                          f"(similarity {similarity:.2f}). Nothing was added.")
            member["templates"] = combined
            member["updated_ms"] = now_ms
            self._save()
            return self._public(member)

    def remove_member(self, member_id: str) -> dict[str, Any]:
        """Consent withdrawn or member removed: the record and all templates are deleted now."""
        with self._lock:
            self._load()
            member = self._members.pop(member_id, None)
            if member is None:
                raise KeyError(member_id)
            self._save()
            return {"member_id": member_id, "display_name": member["display_name"], "templates_deleted": len(member["templates"])}

    def _save(self) -> None:
        payload = json.dumps({"version": 1, "members": list(self._members.values())}, separators=(",", ":")).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(self._protect(payload))
        if sys.platform != "win32":
            os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        self._mtime = self.path.stat().st_mtime
        self.version += 1


def consistency(templates: Sequence[Sequence[float]]) -> float | None:
    """Lowest similarity of any template to the average of the others (None for one template)."""
    if len(templates) < 2:
        return None
    lowest = 1.0
    for index, template in enumerate(templates):
        others = [t for i, t in enumerate(templates) if i != index]
        mean = normalize([sum(values) / len(others) for values in zip(*others)])
        lowest = min(lowest, cosine(template, mean))
    return lowest
