"""Companion-computer identity: pairing, authenticated sessions, health reports.

Pairing: an operator creates a short one-time code for a registered drone
(dashboard, 10 minutes). The companion presents it once and receives its
identity: a companion id and a random 256-bit secret. The Hub keeps the secret
in the credential store (DPAPI on Windows), never in config or API responses.

Sessions: the companion proves it holds the secret with an HMAC over its id,
a timestamp and a fresh nonce (replays and stale clocks are refused) and gets
a short-lived session token for its health reports. Revoking a companion ends
its sessions and deletes its secret.

The same secret derives the MAVLink 2 signing key (`mavlink_signing_key`), so a
commercial unit can sign the FC link with a key Sentinel can reproduce.
Standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_TTL_S = 600
SESSION_TTL_S = 1800
CLOCK_WINDOW_S = 120
REPORTING_WITHIN_S = 10.0
MAX_FAILED_PAIRINGS = 10  # per 10 minutes, then pairing pauses
SESSION_CONTEXT = "sentinel-companion-session-v1"


class PairingError(ValueError):
    pass


class SecretBackend(Protocol):
    def set(self, name: str, value: str) -> None: ...
    def get(self, name: str) -> str | None: ...
    def delete(self, name: str) -> None: ...


@dataclass
class Companion:
    companion_id: str
    drone_id: str
    name: str
    paired_ms: int
    paired_by: str
    revoked_ms: int | None = None
    hardware: dict[str, Any] = field(default_factory=dict)
    last_report_ms: int = 0
    last_session_ms: int = 0
    report: dict[str, Any] = field(default_factory=dict)

    def public(self, now_ms: int) -> dict[str, Any]:
        data = asdict(self)
        age = (now_ms - self.last_report_ms) / 1000 if self.last_report_ms else None
        data["state"] = "revoked" if self.revoked_ms else "reporting" if age is not None and age <= REPORTING_WITHIN_S else "silent"
        data["last_report_age_s"] = None if age is None else round(age, 1)
        return data


def session_mac(secret: bytes, companion_id: str, timestamp_ms: int, nonce: str) -> str:
    message = f"{SESSION_CONTEXT}|{companion_id}|{timestamp_ms}|{nonce}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def mavlink_signing_key(secret: bytes) -> bytes:
    """32-byte MAVLink 2 signing key derived from the companion secret."""
    return hmac.new(secret, b"sentinel-mavlink-signing-v1", hashlib.sha256).digest()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalise_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


class CompanionRegistry:
    """Hub side. `save` persists a companion row; `load` returns saved rows."""

    def __init__(self, secrets_backend: SecretBackend, *, load: Callable[[], list[dict[str, Any]]] = lambda: [],
                 save: Callable[[dict[str, Any]], None] = lambda row: None,
                 audit: Callable[[str, str, dict[str, Any]], None] = lambda actor, action, body: None,
                 clock: Callable[[], float] = time.time) -> None:
        self.secrets = secrets_backend
        self._save = save
        self.audit = audit
        self.clock = clock
        self._lock = threading.Lock()
        self.companions: dict[str, Companion] = {}
        for row in load():
            fields = {key: row[key] for key in Companion.__dataclass_fields__ if key in row}
            companion = Companion(**fields)
            self.companions[companion.companion_id] = companion
        self._codes: dict[str, dict[str, Any]] = {}  # sha256(code) -> {drone_id, expires_ms, by}
        self._sessions: dict[str, dict[str, Any]] = {}  # sha256(token) -> {companion_id, expires_ms}
        self._nonces: dict[str, int] = {}
        self._failures: list[int] = []

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    # ---- operator side -----------------------------------------------------

    def create_code(self, drone_id: str, actor: str) -> dict[str, Any]:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires = self._now_ms() + CODE_TTL_S * 1000
        with self._lock:
            self._codes = {key: value for key, value in self._codes.items() if value["expires_ms"] > self._now_ms()}
            self._codes[_hash(code)] = {"drone_id": drone_id, "expires_ms": expires, "by": actor}
        self.audit(actor, "drone_companion_code_created", {"drone_id": drone_id, "expires_ms": expires})
        return {"code": f"{code[:4]}-{code[4:]}", "expires_ms": expires, "drone_id": drone_id}

    def revoke(self, companion_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            companion = self.companions.get(companion_id)
            if companion is None:
                raise KeyError(companion_id)
            companion.revoked_ms = self._now_ms()
            self._sessions = {key: value for key, value in self._sessions.items() if value["companion_id"] != companion_id}
            self._save(asdict(companion))
        self.secrets.delete(self._secret_name(companion_id))
        self.audit(actor, "drone_companion_revoked", {"companion_id": companion_id})
        return companion.public(self._now_ms())

    def list(self) -> list[dict[str, Any]]:
        now = self._now_ms()
        with self._lock:
            return [companion.public(now) for companion in sorted(self.companions.values(), key=lambda c: -c.paired_ms)]

    def for_drone(self, drone_id: str) -> Companion | None:
        with self._lock:
            active = [c for c in self.companions.values() if c.drone_id == drone_id and not c.revoked_ms]
        return max(active, key=lambda c: c.paired_ms) if active else None

    # ---- companion side ----------------------------------------------------

    def pair(self, code: str, name: str, hardware: dict[str, Any] | None = None) -> dict[str, Any]:
        now = self._now_ms()
        with self._lock:
            self._failures = [ms for ms in self._failures if now - ms < 600_000]
            if len(self._failures) >= MAX_FAILED_PAIRINGS:
                raise PairingError("too many failed pairing attempts; try again in 10 minutes")
            entry = self._codes.pop(_hash(normalise_code(code)), None)
            if entry is None or entry["expires_ms"] < now:
                self._failures.append(now)
                raise PairingError("unknown or expired pairing code")
            companion_id = f"cmp_{secrets.token_hex(6)}"
            secret = secrets.token_bytes(32)
            companion = Companion(companion_id=companion_id, drone_id=entry["drone_id"], name=name.strip()[:60] or companion_id,
                                  paired_ms=now, paired_by=entry["by"], hardware=_clean(hardware or {}))
            # One active companion per drone: pairing a replacement retires the previous one.
            previous = [c for c in self.companions.values() if c.drone_id == companion.drone_id and not c.revoked_ms]
            self.companions[companion_id] = companion
            self._save(asdict(companion))
        for old in previous:
            self.revoke(old.companion_id, "pairing")
        self.secrets.set(self._secret_name(companion_id), base64.b64encode(secret).decode())
        self.audit("companion", "drone_companion_paired", {"companion_id": companion_id, "drone_id": companion.drone_id, "name": companion.name})
        return {"companion_id": companion_id, "drone_id": companion.drone_id, "secret": base64.b64encode(secret).decode()}

    def open_session(self, companion_id: str, timestamp_ms: int, nonce: str, mac: str) -> dict[str, Any]:
        now = self._now_ms()
        with self._lock:
            companion = self.companions.get(companion_id)
            if companion is None or companion.revoked_ms:
                raise PairingError("unknown or revoked companion")
            if abs(now - int(timestamp_ms)) > CLOCK_WINDOW_S * 1000:
                raise PairingError("timestamp outside the allowed window (check the companion's clock)")
            if not (8 <= len(nonce) <= 64) or nonce in self._nonces:
                raise PairingError("nonce missing or already used")
        secret = self._secret(companion_id)
        if secret is None or not hmac.compare_digest(session_mac(secret, companion_id, int(timestamp_ms), nonce), mac):
            raise PairingError("authentication failed")
        token = secrets.token_urlsafe(32)
        expires = now + SESSION_TTL_S * 1000
        with self._lock:
            self._nonces = {key: ms for key, ms in self._nonces.items() if now - ms < 2 * CLOCK_WINDOW_S * 1000}
            self._nonces[nonce] = now
            self._sessions[_hash(token)] = {"companion_id": companion_id, "expires_ms": expires}
            companion.last_session_ms = now
            self._save(asdict(companion))
        return {"token": token, "expires_ms": expires, "drone_id": companion.drone_id}

    def authenticate(self, token: str) -> Companion:
        now = self._now_ms()
        with self._lock:
            session = self._sessions.get(_hash(token or ""))
            if session is None or session["expires_ms"] < now:
                raise PairingError("session expired or unknown")
            companion = self.companions.get(session["companion_id"])
            if companion is None or companion.revoked_ms:
                raise PairingError("companion revoked")
            return companion

    def report(self, token: str, report: dict[str, Any]) -> Companion:
        companion = self.authenticate(token)
        with self._lock:
            companion.last_report_ms = self._now_ms()
            companion.report = _clean(report)
        return companion

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _secret_name(companion_id: str) -> str:
        return f"companion_{companion_id}"

    def _secret(self, companion_id: str) -> bytes | None:
        value = self.secrets.get(self._secret_name(companion_id))
        return base64.b64decode(value) if value else None


def _clean(data: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Bounded copy of a device-supplied report: plain JSON values, short strings."""
    clean: dict[str, Any] = {}
    for key, value in list(data.items())[:40]:
        key = str(key)[:40]
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value[:200]
        elif isinstance(value, dict) and depth < 2:
            clean[key] = _clean(value, depth + 1)
        elif isinstance(value, list):
            clean[key] = [v if isinstance(v, (int, float, bool)) or v is None else str(v)[:120] for v in value[:20]]
    return clean
