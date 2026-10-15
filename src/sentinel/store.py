from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import SEVERITY_RANK, Incident, SecurityEvent

log = logging.getLogger("sentinel.store")

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
CREATE TABLE IF NOT EXISTS outages (
    outage_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outages_time ON outages (started_ms);
CREATE TABLE IF NOT EXISTS visits (
    visit_id TEXT PRIMARY KEY,
    site_track_id TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT,
    camera_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    zone_type TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    dwell_s REAL NOT NULL,
    arrived INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS visits_time ON visits (start_ms);
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_time ON trips (first_seen_ms);
CREATE TABLE IF NOT EXISTS missed_reports (
    report_id TEXT PRIMARY KEY,
    created_ms INTEGER NOT NULL,
    occurred_ms INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    recorded_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS drone_inspections (
    request_id TEXT PRIMARY KEY,
    drone_id TEXT NOT NULL,
    incident_id TEXT,
    created_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drone_inspections_incident ON drone_inspections (incident_id);
CREATE TABLE IF NOT EXISTS drone_flights (
    flight_id TEXT PRIMARY KEY,
    drone_id TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drone_maintenance (
    record_id TEXT PRIMARY KEY,
    drone_id TEXT NOT NULL,
    recorded_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drone_companions (
    companion_id TEXT PRIMARY KEY,
    drone_id TEXT NOT NULL,
    body TEXT NOT NULL
);
"""
VISIT_COLUMNS = ("visit_id", "site_track_id", "label", "kind", "camera_id", "zone_id", "zone_type", "start_ms", "end_ms", "dwell_s", "arrived")
# Incidents whose media and rows outlive normal retention (until `protected_media` days).
PROTECTED = "(status = 'open' OR json_extract(body, '$.protected') = 1 OR json_extract(body, '$.resolution') = 'real_concern')"


class EventStore:
    """Local SQLite history of events, incidents, outages, and operator actions.
    Media files live on disk next to it; rows only hold relative paths.

    Writes never raise: a full disk or a damaged file must not take down camera
    processing. Failures are counted (`write_failures`, `last_error`) so the
    health monitor can report the database as degraded."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.write_failures = 0
        self.last_error = ""
        self.last_write_ok = True
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._db.executescript(SCHEMA)
                self._db.execute("PRAGMA journal_mode=WAL")
                # WAL + NORMAL stays consistent after a crash and only risks the last
                # commit on power loss; FULL fsyncs every event (tens of ms on Windows).
                self._db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            self._db.close()  # release the file so a damaged database can be moved aside
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def quick_check(self) -> str:
        with self._lock:
            return str(self._db.execute("PRAGMA quick_check").fetchone()[0])

    def backup_to(self, target: Path) -> None:
        """Consistent online copy (SQLite backup API), safe while cameras write."""
        destination = sqlite3.connect(target)
        try:
            with self._lock:
                self._db.backup(destination)
        finally:
            destination.close()

    def merge_rows(self, rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        """Add rows salvaged from a damaged copy. Existing rows win (INSERT OR
        IGNORE), so nothing restored from a backup is overwritten. Returns the
        number of rows that were new, per table."""
        merged: dict[str, int] = {}
        with self._lock, self._db:
            known = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table, records in rows.items():
                if table not in known or not records:
                    continue
                columns = [row[1] for row in self._db.execute(f'PRAGMA table_info("{table}")')]
                count = 0
                for record in records:
                    keys = [key for key in record if key in columns]
                    cursor = self._db.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({",".join(keys)}) VALUES ({",".join("?" * len(keys))})', [record[key] for key in keys]
                    )
                    count += cursor.rowcount
                merged[table] = count
        return merged

    def _write(self, sql: str, params: tuple) -> bool:
        try:
            with self._lock, self._db:
                self._db.execute(sql, params)
        except sqlite3.Error as error:
            self.write_failures += 1
            self.last_write_ok = False
            if self.last_error != str(error):
                log.error("database write failed: %s", error)
            self.last_error = str(error)
            return False
        self.last_write_ok = True
        return True

    def add_event(self, event: SecurityEvent) -> None:
        self._write(
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
        with self._lock:
            row = self._db.execute("SELECT body FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return
        body = json.loads(row["body"])
        if snapshot_path:
            body["snapshot_path"] = snapshot_path
        if clip_path:
            body["clip_path"] = clip_path
        self._write("UPDATE events SET body = ? WHERE event_id = ?", (json.dumps(body), event_id))

    def upsert_incident(self, incident: Incident) -> None:
        self._write(
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

    def upsert_outage(self, outage: dict[str, Any]) -> None:
        self._write(
            "INSERT OR REPLACE INTO outages VALUES (?,?,?,?,?,?,?)",
            (outage["outage_id"], outage["scope"], outage["camera_id"], outage["kind"], outage["started_ms"], outage["ended_ms"], json.dumps(outage)),
        )

    def list_outages(self, *, since_ms: int | None = None, camera_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses, params = [], []
        if since_ms is not None:
            clauses.append("(ended_ms IS NULL OR ended_ms >= ?)")
            params.append(since_ms)
        if camera_id:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM outages {where} ORDER BY started_ms DESC LIMIT ?", params).fetchall()
        return [json.loads(row["body"]) for row in rows]

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
        self._write(
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

    # ---- activity statistics (no images, no positions, no identities) --------

    def add_activity(self, visits: list[dict[str, Any]], trips: list[dict[str, Any]]) -> None:
        for visit in visits:
            self._write(f"INSERT OR REPLACE INTO visits VALUES ({','.join('?' * len(VISIT_COLUMNS))})",
                        tuple(int(visit[key]) if key == "arrived" else visit[key] for key in VISIT_COLUMNS))
        for trip in trips:
            self._write("INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?)",
                        (trip["trip_id"], trip["label"], trip["first_seen_ms"], trip["last_seen_ms"], json.dumps(trip)))

    def list_visits(self, *, since_ms: int | None = None, until_ms: int | None = None, label: str | None = None, limit: int = 100_000) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if since_ms is not None:
            clauses.append("start_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("start_ms < ?")
            params.append(until_ms)
        if label:
            clauses.append("label = ?")
            params.append(label)
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM visits WHERE {' AND '.join(clauses)} ORDER BY start_ms LIMIT ?", params + [limit]).fetchall()
        return [dict(row) | {"arrived": bool(row["arrived"])} for row in rows]

    def list_trips(self, *, since_ms: int | None = None, until_ms: int | None = None, limit: int = 50_000) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if since_ms is not None:
            clauses.append("first_seen_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("first_seen_ms < ?")
            params.append(until_ms)
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM trips WHERE {' AND '.join(clauses)} ORDER BY first_seen_ms LIMIT ?", params + [limit]).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def purge_activity(self, before_ms: int) -> int:
        try:
            with self._lock, self._db:
                removed = self._db.execute("DELETE FROM visits WHERE start_ms < ?", (before_ms,)).rowcount
                removed += self._db.execute("DELETE FROM trips WHERE first_seen_ms < ?", (before_ms,)).rowcount
            return removed
        except sqlite3.Error as error:
            log.error("activity purge failed: %s", error)
            return 0

    # ---- investigation ---------------------------------------------------------

    def search_incidents(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        camera_id: str | None = None,
        zone_id: str | None = None,
        label: str | None = None,
        min_severity: str | None = None,
        outcome: str | None = None,
        rule_id: str | None = None,
        text: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if since_ms is not None:
            clauses.append("updated_ms >= ?")
            params.append(since_ms)
        if until_ms is not None:
            clauses.append("opened_ms <= ?")
            params.append(until_ms)
        for key, value in (("cameras", camera_id), ("zones", zone_id), ("labels", label)):
            if value:
                clauses.append(f"EXISTS (SELECT 1 FROM json_each(body, '$.{key}') WHERE value = ?)")
                params.append(value)
        if min_severity:
            clauses.append("CASE severity WHEN 'info' THEN 0 WHEN 'low' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END >= ?")
            params.append(SEVERITY_RANK.get(min_severity, 0))
        if outcome == "unreviewed":
            clauses.append("COALESCE(json_extract(body, '$.resolution'), '') = ''")
        elif outcome:
            clauses.append("json_extract(body, '$.resolution') = ?")
            params.append(outcome)
        if rule_id:
            clauses.append("incident_id IN (SELECT incident_id FROM events WHERE json_extract(body, '$.rule_id') = ?)")
            params.append(rule_id)
        if text:
            clauses.append("(json_extract(body, '$.title') LIKE ? OR incident_id IN (SELECT incident_id FROM events WHERE summary LIKE ?))")
            params += [f"%{text}%", f"%{text}%"]
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM incidents WHERE {' AND '.join(clauses)} ORDER BY opened_ms DESC LIMIT ?", params).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def rule_events(self, since_ms: int) -> list[dict[str, Any]]:
        """Rule matches (rule id, incident, camera, severity) since a time, for reports."""
        with self._lock:
            rows = self._db.execute(
                "SELECT json_extract(body, '$.rule_id') AS rule_id, incident_id, camera_id, severity FROM events "
                "WHERE timestamp_ms >= ? AND json_extract(body, '$.rule_id') != ''", (since_ms,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_missed_report(self, report: dict[str, Any]) -> None:
        self._write("INSERT OR REPLACE INTO missed_reports VALUES (?,?,?,?,?)",
                    (report["report_id"], report["created_ms"], report["occurred_ms"], report["camera_id"], json.dumps(report)))

    def list_missed_reports(self, *, since_ms: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM missed_reports WHERE occurred_ms >= ? ORDER BY occurred_ms DESC LIMIT ?",
                                    (since_ms or 0, limit)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def record_evidence(self, path: str, sha256: str, size: int) -> None:
        """First hash wins: evidence is fingerprinted when it is finalised, and a
        later different hash means the file changed afterwards."""
        self._write("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)", (path, sha256, size, int(time.time() * 1000)))

    def evidence_hashes(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM evidence WHERE path IN ({','.join('?' * len(paths))})", paths).fetchall()
        return {row["path"]: dict(row) for row in rows}

    def protected_media(self) -> set[str]:
        """Media of incidents that are still open, marked protected, or resolved
        as a real concern. Retention and disk-space purges skip these files."""
        keep: set[str] = set()
        with self._lock:
            for row in self._db.execute(f"SELECT body FROM incidents WHERE {PROTECTED}"):
                incident = json.loads(row["body"])
                keep.update(incident.get("clip_paths", []))
                if incident.get("snapshot_path"):
                    keep.add(incident["snapshot_path"])
            for row in self._db.execute(
                f"SELECT body FROM events WHERE incident_id IN (SELECT incident_id FROM incidents WHERE {PROTECTED})"
            ):
                event = json.loads(row["body"])
                keep.update(path for path in (event.get("snapshot_path"), event.get("clip_path")) if path)
            for row in self._db.execute(
                f"SELECT body FROM drone_inspections WHERE incident_id IN (SELECT incident_id FROM incidents WHERE {PROTECTED})"
            ):
                keep.update(json.loads(row["body"]).get("evidence_paths", []))
        return keep

    # ---- drone records -------------------------------------------------------
    # Inspections follow incident retention. Flight and maintenance logs are
    # aircraft records (no images, no people) and are kept.

    def upsert_drone_inspection(self, inspection: dict[str, Any]) -> bool:
        return self._write(
            "INSERT OR REPLACE INTO drone_inspections VALUES (?,?,?,?,?,?)",
            (inspection["request_id"], inspection.get("drone_id", ""), inspection.get("incident_id") or None, int(inspection["created_ms"]),
             inspection["status"], json.dumps(inspection)),
        )

    def list_drone_inspections(self, *, drone_id: str | None = None, incident_id: str | None = None,
                               statuses: tuple[str, ...] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (("drone_id", drone_id), ("incident_id", incident_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM drone_inspections {where} ORDER BY created_ms DESC LIMIT ?", (*params, limit)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def upsert_drone_flight(self, flight: dict[str, Any]) -> bool:
        return self._write("INSERT OR REPLACE INTO drone_flights VALUES (?,?,?,?,?)",
                    (flight["flight_id"], flight["drone_id"], int(flight["started_ms"]), flight.get("ended_ms"), json.dumps(flight)))

    def list_drone_flights(self, *, drone_id: str | None = None, open_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        if drone_id is not None:
            clauses.append("drone_id = ?")
            params.append(drone_id)
        if open_only:
            clauses.append("ended_ms IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM drone_flights {where} ORDER BY started_ms DESC LIMIT ?", (*params, limit)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def add_drone_maintenance(self, record: dict[str, Any]) -> bool:
        return self._write("INSERT OR REPLACE INTO drone_maintenance VALUES (?,?,?,?,?)",
                    (record["record_id"], record["drone_id"], int(record["recorded_ms"]), record["kind"], json.dumps(record)))

    def list_drone_maintenance(self, *, drone_id: str | None = None, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (("drone_id", drone_id), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM drone_maintenance {where} ORDER BY recorded_ms DESC LIMIT ?", (*params, limit)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def save_drone_companion(self, companion: dict[str, Any]) -> None:
        self._write("INSERT OR REPLACE INTO drone_companions VALUES (?,?,?)", (companion["companion_id"], companion["drone_id"], json.dumps(companion)))

    def list_drone_companions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM drone_companions").fetchall()
        return [json.loads(row["body"]) for row in rows]

    def purge(
        self,
        *,
        events_before_ms: int,
        media_before_ms: int,
        media_root: Path,
        protected_before_ms: int | None = None,
    ) -> dict[str, int]:
        """Apply retention: drop old rows, then delete media older than the media
        cutoff even when the rows referencing it are still retained. Protected
        incidents (see `protected_media`) keep their rows and media until
        `protected_before_ms`."""
        protected_before_ms = events_before_ms if protected_before_ms is None else protected_before_ms
        protected = self.protected_media()
        try:
            with self._lock, self._db:
                events = self._db.execute(
                    "DELETE FROM events WHERE timestamp_ms < ? AND (timestamp_ms < ? OR incident_id IS NULL OR incident_id NOT IN "
                    f"(SELECT incident_id FROM incidents WHERE {PROTECTED}))",
                    (events_before_ms, protected_before_ms),
                ).rowcount
                incidents = self._db.execute(
                    f"DELETE FROM incidents WHERE status = 'closed' AND updated_ms < ? AND (updated_ms < ? OR NOT {PROTECTED})",
                    (events_before_ms, protected_before_ms),
                ).rowcount
                self._db.execute("DELETE FROM audit WHERE timestamp_ms < ?", (events_before_ms,))
                self._db.execute(
                    "DELETE FROM drone_inspections WHERE created_ms < ? AND status NOT IN ('pending_approval', 'approved', 'in_progress') AND "
                    f"(created_ms < ? OR incident_id IS NULL OR incident_id NOT IN (SELECT incident_id FROM incidents WHERE {PROTECTED}))",
                    (events_before_ms, protected_before_ms),
                )
                self._db.execute("DELETE FROM outages WHERE ended_ms IS NOT NULL AND ended_ms < ?", (events_before_ms,))
        except sqlite3.Error as error:
            log.error("retention purge of rows failed: %s", error)
            events = incidents = 0
        media = 0
        if media_root.exists():
            cutoff_s, protected_cutoff_s = media_before_ms / 1000, protected_before_ms / 1000
            for path in media_root.rglob("*"):
                if not path.is_file():
                    continue
                modified = path.stat().st_mtime
                if modified >= cutoff_s:
                    continue
                if modified >= protected_cutoff_s and path.relative_to(media_root).as_posix() in protected:
                    continue
                path.unlink(missing_ok=True)
                media += 1
        return {"events": events, "incidents": incidents, "media_files": media}
