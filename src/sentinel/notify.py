from __future__ import annotations

import logging
import queue
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import Notification

log = logging.getLogger("sentinel.notify")


class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None: ...


class LogNotifier:
    name = "log"

    def send(self, notification: Notification) -> None:
        log.warning("NOTIFY [%s] %s | %s", notification.severity, notification.title, notification.message.replace("\n", " / "))


class NtfyNotifier:
    """Push via ntfy (self-hosted or ntfy.sh). The topic name acts as a password
    on public servers, so it comes from the environment. Snapshots are only
    attached when explicitly enabled because that sends imagery off-site."""

    name = "ntfy"

    def __init__(self, server: str, topic: str, *, token: str = "", click_url: str = "", attach_snapshot: bool = False, media_root: Path | None = None) -> None:
        self.url = f"{server.rstrip('/')}/{topic}"
        self.token = token
        self.click_url = click_url
        self.attach_snapshot = attach_snapshot
        self.media_root = media_root

    def send(self, notification: Notification) -> None:
        headers = {
            "Title": notification.title.encode("ascii", "replace").decode(),
            "Priority": str(notification.priority),
            "Tags": "rotating_light" if notification.severity == "high" else "warning",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.click_url:
            headers["Click"] = f"{self.click_url.rstrip('/')}/#incident={notification.incident_id}"
        body = notification.message.encode("utf-8")
        snapshot = self._snapshot_file(notification)
        if snapshot is not None:
            headers["Filename"] = snapshot.name
            headers["Message"] = notification.message.replace("\n", " / ").encode("ascii", "replace").decode()
            body = snapshot.read_bytes()
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()

    def _snapshot_file(self, notification: Notification) -> Path | None:
        if not (self.attach_snapshot and self.media_root and notification.snapshot_path):
            return None
        path = (self.media_root / notification.snapshot_path).resolve()
        return path if path.is_file() and self.media_root.resolve() in path.parents else None


class CallbackNotifier:
    def __init__(self, name: str, callback: Callable[[Notification], None]) -> None:
        self.name = name
        self.callback = callback

    def send(self, notification: Notification) -> None:
        self.callback(notification)


class NotificationDispatcher:
    """Sends on a background thread so a slow push server never stalls video processing."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers
        self.sent: list[dict[str, Any]] = []
        self._queue: queue.Queue[Notification | None] = queue.Queue(maxsize=200)
        self._thread = threading.Thread(target=self._run, name="sentinel-notify", daemon=True)
        self._thread.start()

    def submit(self, notification: Notification) -> None:
        try:
            self._queue.put_nowait(notification)
        except queue.Full:
            log.error("notification queue full; dropping %s", notification.incident_id)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            notification = self._queue.get()
            if notification is None:
                return
            outcomes = {}
            for notifier in self.notifiers:
                try:
                    notifier.send(notification)
                    outcomes[notifier.name] = "sent"
                except Exception as error:  # one failed channel must not block the others
                    log.error("notifier %s failed: %s", notifier.name, error)
                    outcomes[notifier.name] = f"failed: {error}"
            self.sent.append(notification.as_dict() | {"channels": outcomes})
            del self.sent[:-200]


def build_notifiers(
    config: dict[str, Any], media_root: Path, mqtt_publish: Callable[[str, dict], None] | None,
    secret: Callable[[str], str | None] = lambda name: None,
) -> list[Notifier]:
    """`secret` looks up credential-store entries: a topic typed into the
    dashboard is stored there (`topic_credential`), never in the config."""
    notifiers: list[Notifier] = [LogNotifier()]
    for channel in config.get("alerts", {}).get("channels", []):
        if not channel.get("enabled", True):
            continue
        if channel["type"] == "ntfy":
            topic = secret(channel["topic_credential"]) if channel.get("topic_credential") else channel.get("topic")
            if not topic:
                log.error("ntfy channel has no topic (credential %s missing); channel skipped", channel.get("topic_credential", ""))
                continue
            notifiers.append(
                NtfyNotifier(
                    channel.get("server", "https://ntfy.sh"),
                    topic,
                    token=channel.get("token", ""),
                    click_url=channel.get("click_url", ""),
                    attach_snapshot=bool(channel.get("attach_snapshot", False)),
                    media_root=media_root,
                )
            )
        elif channel["type"] == "mqtt" and mqtt_publish:
            notifiers.append(CallbackNotifier("mqtt", lambda n: mqtt_publish("alerts", n.as_dict())))
    return notifiers
