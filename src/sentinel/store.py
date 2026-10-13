from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import SEVERITY_RANK, Incident, SecurityEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    timestamp_ms INTEGER NOT NULL,
    started_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    severity_rank INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    zone_id TEXT,
    site_track_id TEXT,
    incident_id TEXT,
    confidence REAL,
    summary TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events (timestamp_ms);
CREATE INDEX IF NOT EXISTS events_incident ON events (incident_id);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    opened_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_time ON incidents (opened_ms);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    body TEXT NOT NULL
);
"""


class EventStore:
    """Local SQLite history of events, incidents, and operator actions. Media
    files live on disk next to it; rows only hold relative paths."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add_event(self, event: SecurityEvent) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.timestamp_ms,
                    event.started_ms,
                    event.event_type,
                    event.severity,
                    SEVERITY_RANK[event.severity],
                    event.camera_id,
                    event.zone_id,
                    event.site_track_id,
                    event.incident_id,
                    event.confidence,
                    event.summary,
                    json.dumps(event.as_dict()),
                ),
            )

    def update_event_media(self, event_id: str, *, snapshot_path: str = "", clip_path: str = "") -> None:
        with self._lock, self._db:
            row = self._db.execute("SELECT body FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                return
            body = json.loads(row["body"])
            if snapshot_path:
                body["snapshot_path"] = snapshot_path
            if clip_path:
                body["clip_path"] = clip_path
            self._db.execute("UPDATE events SET body = ? WHERE event_id = ?", (json.dumps(body), event_id))

    def upsert_incident(self, incident: Incident) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?)",
                (
                    incident.incident_id,
                    incident.opened_ms,
                    incident.updated_ms,
                    incident.status,
                    incident.severity,
                    json.dumps(incident.as_dict()),
                ),
            )

    def list_events(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        camera_id: str | None = None,
        min_severity: str | None = None,
        incident_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if since_ms is not None:
            clauses.append("timestamp_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("timestamp_ms <= ?")
            params.append(until_ms)
        if camera_id:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if min_severity:
            clauses.append("severity_rank >= ?")
            params.append(SEVERITY_RANK.get(min_severity, 0))
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(
                f"SELECT body FROM events {where} ORDER BY timestamp_ms DESC LIMIT ?", params
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def list_incidents(self, *, status: str | None = None, since_ms: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since_ms is not None:
            clauses.append("updated_ms >= ?")
            params.append(since_ms)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self._lock:
            rows = self._db.execute(
                f"SELECT body FROM incidents {where} ORDER BY opened_ms DESC LIMIT ?", params
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def audit(self, actor: str, action: str, body: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO audit (timestamp_ms, actor, action, body) VALUES (?,?,?,?)",
                (int(time.time() * 1000), actor, action, json.dumps(body)),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT timestamp_ms, actor, action, body FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"timestamp_ms": row["timestamp_ms"], "actor": row["actor"], "action": row["action"], "body": json.loads(row["body"])}
            for row in rows
        ]

    def purge(self, *, events_before_ms: int, media_before_ms: int, media_root: Path) -> dict[str, int]:
        """Apply retention: drop old rows, then delete media older than the media
        cutoff even when the rows referencing it are still retained."""
        with self._lock, self._db:
            events = self._db.execute("DELETE FROM events WHERE timestamp_ms < ?", (events_before_ms,)).rowcount
            incidents = self._db.execute(
                "DELETE FROM incidents WHERE status = 'closed' AND updated_ms < ?", (events_before_ms,)
            ).rowcount
            self._db.execute("DELETE FROM audit WHERE timestamp_ms < ?", (events_before_ms,))
        media = 0
        if media_root.exists():
            cutoff_s = media_before_ms / 1000
            for path in media_root.rglob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff_s:
                    path.unlink()
                    media += 1
        return {"events": events, "incidents": incidents, "media_files": media}
