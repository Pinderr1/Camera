"""Conversations, kept on this hub only (`<storage>/assistant/<id>.json`).

Each conversation holds what the person saw (`display`), what the AI provider
was sent (`provider_messages`, append-only so cached prefixes and thinking
blocks stay valid), the address aliases, proposals awaiting confirmation, and
the built-in guide's small state (for example the address it last diagnosed)."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_ID = re.compile(r"^[a-f0-9]{32}$")
MAX_CONVERSATIONS = 200


class ConversationNotFound(KeyError):
    pass


def new_conversation() -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {"id": uuid.uuid4().hex, "created_ms": now, "updated_ms": now, "title": "", "display": [], "provider_messages": [],
            "aliases": {}, "pending": {}, "tested_rules": [], "guide": {}}


class ConversationStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.RLock()

    def _path(self, conversation_id: str) -> Path:
        if not _ID.match(conversation_id or ""):
            raise ConversationNotFound(conversation_id)
        return self.directory / f"{conversation_id}.json"

    def create(self) -> dict[str, Any]:
        conversation = new_conversation()
        self.save(conversation)
        self._prune()
        return conversation

    def load(self, conversation_id: str) -> dict[str, Any]:
        path = self._path(conversation_id)
        with self._lock:
            if not path.is_file():
                raise ConversationNotFound(conversation_id)
            try:
                return new_conversation() | json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ConversationNotFound(conversation_id) from None

    def save(self, conversation: dict[str, Any]) -> None:
        conversation["updated_ms"] = int(time.time() * 1000)
        path = self._path(conversation["id"])
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(conversation, default=str), encoding="utf-8")
            temporary.replace(path)

    def delete(self, conversation_id: str) -> None:
        path = self._path(conversation_id)
        with self._lock:
            if not path.is_file():
                raise ConversationNotFound(conversation_id)
            path.unlink()

    def list(self) -> list[dict[str, Any]]:
        items = []
        with self._lock:
            for path in self.directory.glob("*.json") if self.directory.is_dir() else []:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                items.append({"id": data["id"], "title": data.get("title") or "New conversation", "updated_ms": data.get("updated_ms", 0),
                              "messages": len(data.get("display", []))})
        return sorted(items, key=lambda item: -item["updated_ms"])

    def _prune(self) -> None:
        for item in self.list()[MAX_CONVERSATIONS:]:
            self.delete(item["id"])
