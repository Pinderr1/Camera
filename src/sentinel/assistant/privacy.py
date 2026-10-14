"""What leaves the hub when an external AI provider is used.

By default the provider never sees network addresses: each IPv4 address or
network in text sent to it is replaced by a stable alias ([ip-1], [net-1]) kept
in the conversation, and aliases in the provider's answer are turned back into
the real values only for display on this hub. Tools accept either form.

Passwords, tokens and credential-bearing URLs typed into the chat are removed
before anything is stored or sent: camera passwords belong in Sentinel's
credential forms, never in the conversation."""

from __future__ import annotations

import re
from typing import Any

_IP = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(/\d{1,2})?(?!\d|\.\d)")  # a sentence-ending period still ends the address
_ALIAS = re.compile(r"\[(ip|net)-(\d+)\]")
_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@", re.I)
_SECRET_PHRASES = re.compile(
    r"(?i)\b(pass(?:word|wd|code)?|pwd|pin|token|api[ _-]?key|secret)\b(\s*(?:is|=|:|was|to)\s*)([\"']?)([^\s\"',;]{3,})\3")
_KEY_LIKE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{32,})\b")
REDACTED = "[removed]"


def scrub_secrets(text: str) -> tuple[str, bool]:
    """Remove passwords and keys from free text. Returns (text, whether anything was removed)."""
    original = text
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)
    text = _SECRET_PHRASES.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _KEY_LIKE.sub(REDACTED, text)
    return text, text != original


def _is_address(value: str) -> bool:
    parts = value.split(".")
    return all(part.isdigit() and int(part) <= 255 for part in parts) and parts[0] not in ("0", "255")


class Aliases:
    """Per-conversation mapping between real addresses and the aliases the provider sees."""

    def __init__(self, table: dict[str, str] | None = None) -> None:
        self.to_alias: dict[str, str] = dict(table or {})
        self.to_real = {alias: real for real, alias in self.to_alias.items()}

    def as_dict(self) -> dict[str, str]:
        return dict(self.to_alias)

    def hide(self, text: str) -> str:
        def replace(match: re.Match) -> str:
            address, prefix = match.group(1), match.group(2) or ""
            if not _is_address(address):
                return match.group(0)
            real = address + prefix
            if real not in self.to_alias:
                kind = "net" if prefix else "ip"
                alias = f"[{kind}-{sum(1 for a in self.to_alias.values() if a.startswith('[' + kind)) + 1}]"
                self.to_alias[real] = alias
                self.to_real[alias] = real
            return self.to_alias[real]

        return _IP.sub(replace, text)

    def reveal(self, text: str) -> str:
        return _ALIAS.sub(lambda m: self.to_real.get(m.group(0), m.group(0)), text)

    def resolve(self, value: str) -> str:
        """A tool argument: an alias becomes the real address; anything else passes through."""
        value = value.strip()
        return self.to_real.get(value, value)

    def hide_data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.hide(value)
        if isinstance(value, list):
            return [self.hide_data(item) for item in value]
        if isinstance(value, dict):
            return {key: self.hide_data(item) for key, item in value.items()}
        return value


def untrusted(text: Any, limit: int = 80) -> str:
    """Names reported by devices or typed into configs are data, not instructions:
    printable characters only, bounded length."""
    cleaned = "".join(ch for ch in str(text or "") if ch.isprintable())
    return cleaned[:limit]
