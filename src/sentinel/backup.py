"""Backup, verification, restore, and startup recovery of Sentinel's state.

A backup is one zip in `<storage>/backups/` holding the editable site config
(`site.json`, with `${ENV}` placeholders unresolved), runtime settings, the
SQLite database (a consistent online copy), and the property-map image, plus
a `manifest.json` with SHA-256 checksums. Media (clips, snapshots) is not
included: it is large and short-lived by design. Camera passwords are only
included on request, and even then stay DPAPI-encrypted to the Windows account
that saved them (a 0600 file elsewhere).

Backups are written locally and never leave the machine through the API: the
config inside contains stream addresses, which the API treats as write-only."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import EventStore

log = logging.getLogger("sentinel.backup")
FORMAT = 1
PARTS = ("config", "settings", "database", "assets", "credentials")
_PART_FILES = {"config": "site.json", "settings": "settings.json", "database": "sentinel.db", "credentials": "credentials.json"}


class BackupError(RuntimeError):
    pass


def backups_dir(storage_dir: str | Path) -> Path:
    return Path(storage_dir) / "backups"


def create_backup(
    storage_dir: str | Path,
    *,
    config: dict[str, Any] | None = None,
    store: EventStore | None = None,
    label: str = "manual",
    include_credentials: bool = False,
    keep: int | None = None,
) -> dict[str, Any]:
    storage_dir = Path(storage_dir)
    target_dir = backups_dir(storage_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    label = "".join(c for c in label.lower() if c.isalnum() or c in "-_")[:24] or "manual"
    created_ms = int(time.time() * 1000)
    name = f"sentinel-{datetime.fromtimestamp(created_ms / 1000):%Y%m%dT%H%M%S}-{created_ms % 1000:03d}-{label}.zip"
    with tempfile.TemporaryDirectory(dir=target_dir) as work:
        work_dir = Path(work)
        files: dict[str, Path] = {}
        site = config if config is not None else _read_json(storage_dir / "site.json")
        if site is not None:
            files["site.json"] = work_dir / "site.json"
            files["site.json"].write_text(json.dumps({k: v for k, v in site.items() if not k.startswith("_")}, indent=1), encoding="utf-8")
        if (storage_dir / "settings.json").is_file():
            files["settings.json"] = storage_dir / "settings.json"
        database = storage_dir / "sentinel.db"
        if store is not None or database.is_file():
            files["sentinel.db"] = work_dir / "sentinel.db"
            if store is not None:
                store.backup_to(files["sentinel.db"])
            else:
                _sqlite_copy(database, files["sentinel.db"])
        for asset in sorted((storage_dir / "assets").glob("*")) if (storage_dir / "assets").is_dir() else []:
            if asset.is_file():
                files[f"assets/{asset.name}"] = asset
        if include_credentials and (storage_dir / "credentials.json").is_file():
            files["credentials.json"] = storage_dir / "credentials.json"
        manifest = {
            "format": FORMAT, "created_ms": created_ms, "label": label,
            "site_id": (site or {}).get("site", {}).get("id", ""),
            "includes_credentials": "credentials.json" in files,
            "files": {arcname: {"sha256": _sha256(path), "bytes": path.stat().st_size} for arcname, path in files.items()},
        }
        partial = target_dir / (name + ".partial")
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=1))
            for arcname, path in files.items():
                archive.write(path, arcname)
        partial.replace(target_dir / name)
    if keep is not None:
        prune_backups(storage_dir, label=label, keep=keep)
    log.info("backup written: %s", name)
    return _describe(target_dir / name, manifest)


def list_backups(storage_dir: str | Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(backups_dir(storage_dir).glob("sentinel-*.zip"), reverse=True):
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            result.append(_describe(path, manifest))
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError):
            result.append({"name": path.name, "valid": False, "size_bytes": path.stat().st_size})
    return result


def prune_backups(storage_dir: str | Path, *, label: str, keep: int) -> list[str]:
    removed = []
    matching = sorted(backups_dir(storage_dir).glob(f"sentinel-*-{label}.zip"), reverse=True)
    for path in matching[max(0, keep):]:
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def resolve_backup(storage_dir: str | Path, name: str) -> Path:
    """Backup names come from API clients: only plain file names inside the backups folder."""
    if "/" in name or "\\" in name or not name.startswith("sentinel-") or not name.endswith(".zip"):
        raise BackupError("unknown backup")
    path = backups_dir(storage_dir) / name
    if not path.is_file():
        raise BackupError("unknown backup")
    return path


def verify_backup(path: str | Path) -> dict[str, Any]:
    problems: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                problems.append(f"damaged entry {bad}")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != FORMAT:
                problems.append(f"unsupported backup format {manifest.get('format')!r}")
            for arcname, expected in manifest.get("files", {}).items():
                if hashlib.sha256(archive.read(arcname)).hexdigest() != expected["sha256"]:
                    problems.append(f"{arcname} does not match its checksum")
            with tempfile.TemporaryDirectory() as work:
                if "sentinel.db" in manifest.get("files", {}):
                    database = Path(work) / "sentinel.db"
                    database.write_bytes(archive.read("sentinel.db"))
                    result = _integrity(database)
                    if result != "ok":
                        problems.append(f"database integrity check: {result}")
            if "site.json" in manifest.get("files", {}):
                from .config import validate_config

                try:
                    validate_config(json.loads(archive.read("site.json")))
                except ValueError as error:
                    problems.append(f"site config is invalid: {error}")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as error:
        problems.append(f"not a readable Sentinel backup ({type(error).__name__})")
    return {"ok": not problems, "problems": problems, "manifest": manifest}


def restore_backup(path: str | Path, storage_dir: str | Path, *, parts: tuple[str, ...] = ("config", "settings", "database", "assets")) -> dict[str, Any]:
    """Offline restore: the service must be stopped (the database is replaced).
    A `pre-restore` backup of the current state is written first."""
    storage_dir = Path(storage_dir)
    unknown = set(parts) - set(PARTS)
    if unknown:
        raise BackupError(f"unknown part(s): {', '.join(sorted(unknown))}")
    check = verify_backup(path)
    if not check["ok"]:
        raise BackupError("backup failed verification: " + "; ".join(check["problems"]))
    safety = None
    if any((storage_dir / name).exists() for name in ("site.json", "settings.json", "sentinel.db")):
        safety = create_backup(storage_dir, label="pre-restore", include_credentials="credentials" in parts)["name"]
    restored = []
    with zipfile.ZipFile(path) as archive:
        names = set(check["manifest"]["files"])
        for part in parts:
            if part == "assets":
                members = [name for name in names if name.startswith("assets/")]
                if members:
                    assets = storage_dir / "assets"
                    if assets.exists():
                        shutil.rmtree(assets)
                    assets.mkdir(parents=True)
                    for member in members:
                        (assets / Path(member).name).write_bytes(archive.read(member))
                    restored.append(part)
                continue
            member = _PART_FILES[part]
            if member not in names:
                continue
            target = storage_dir / member
            if part == "database":
                for suffix in ("-wal", "-shm"):
                    Path(str(target) + suffix).unlink(missing_ok=True)
            temporary = target.with_name(target.name + ".restoring")
            temporary.write_bytes(archive.read(member))
            temporary.replace(target)
            restored.append(part)
    log.warning("restored %s from %s", ", ".join(restored), Path(path).name)
    return {"restored": restored, "safety_backup": safety, "backup": Path(path).name}


def stage_database_restore(path: str | Path, storage_dir: str | Path) -> dict[str, Any]:
    """For a running service: the database is swapped in at the next start."""
    check = verify_backup(path)
    if not check["ok"]:
        raise BackupError("backup failed verification: " + "; ".join(check["problems"]))
    if "sentinel.db" not in check["manifest"]["files"]:
        raise BackupError("this backup has no database")
    pending = Path(storage_dir) / "restore_pending"
    pending.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        (pending / "sentinel.db").write_bytes(archive.read("sentinel.db"))
    (pending / "source.json").write_text(json.dumps({"backup": Path(path).name, "staged_ms": int(time.time() * 1000)}), encoding="utf-8")
    return {"staged": True, "backup": Path(path).name}


def open_store(storage_dir: str | Path) -> tuple[EventStore, dict[str, Any] | None]:
    """Open the event database, applying a staged restore first, and recover
    from a damaged file: the bad copy is quarantined in `<storage>/recovery/`
    and the newest backup that verifies is restored (or an empty database is
    started). Returns (store, report or None)."""
    storage_dir = Path(storage_dir)
    database = storage_dir / "sentinel.db"
    report = _apply_pending_restore(storage_dir, database)
    try:
        store = EventStore(database)
    except sqlite3.DatabaseError as error:
        return _recover(storage_dir, database, f"{type(error).__name__}: {error}")
    try:
        check = store.quick_check()
    except sqlite3.DatabaseError as error:
        check = f"{type(error).__name__}: {error}"
    if check != "ok":
        store.close()
        return _recover(storage_dir, database, check)
    return store, report


def _apply_pending_restore(storage_dir: Path, database: Path) -> dict[str, Any] | None:
    pending = storage_dir / "restore_pending"
    staged = pending / "sentinel.db"
    if not staged.is_file():
        return None
    source = _read_json(pending / "source.json") or {}
    if database.exists():
        _quarantine(storage_dir, database, "replaced")
    staged.replace(database)
    shutil.rmtree(pending, ignore_errors=True)
    log.warning("database restored from %s (staged restore)", source.get("backup", "a backup"))
    return {"action": "staged_restore", "backup": source.get("backup", "")}


def _recover(storage_dir: Path, database: Path, problem: str) -> tuple[EventStore, dict[str, Any]]:
    quarantined = _quarantine(storage_dir, database, "corrupt")
    log.error("event database is damaged (%s); moved to %s", problem, quarantined)
    for backup in list_backups(storage_dir):
        if not backup.get("valid", True) or "sentinel.db" not in backup.get("files", []):
            continue
        path = backups_dir(storage_dir) / backup["name"]
        if verify_backup(path)["ok"]:
            with zipfile.ZipFile(path) as archive:
                database.write_bytes(archive.read("sentinel.db"))
            log.warning("event database restored from backup %s", backup["name"])
            return EventStore(database), {
                "action": "restored_from_backup", "backup": backup["name"], "backup_created_ms": backup["created_ms"],
                "problem": problem[:300], "quarantined": quarantined.name,
            }
    log.error("no usable backup: starting with an empty event database")
    return EventStore(database), {"action": "started_empty", "problem": problem[:300], "quarantined": quarantined.name}


def _quarantine(storage_dir: Path, database: Path, why: str) -> Path:
    recovery = storage_dir / "recovery"
    recovery.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    target = recovery / f"sentinel.db.{stamp}.{why}"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(database) + suffix)
        if source.exists():
            source.replace(Path(str(target) + suffix))
    return target


def _describe(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": path.name, "valid": True, "created_ms": manifest.get("created_ms", 0), "label": manifest.get("label", ""),
        "site_id": manifest.get("site_id", ""), "includes_credentials": manifest.get("includes_credentials", False),
        "files": sorted(manifest.get("files", {})), "size_bytes": path.stat().st_size,
    }


def _sqlite_copy(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _integrity(database: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        return str(error)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
