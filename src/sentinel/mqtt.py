from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("sentinel.mqtt")


class MqttPublisher:
    """Mirrors bus traffic to MQTT (Home Assistant, Node-RED, other consumers).
    Same conventions as the senior_safety bridge: credentials come from
    MQTT_USERNAME / MQTT_PASSWORD, never from config files."""

    def __init__(self, settings: dict[str, Any], site_id: str) -> None:
        import paho.mqtt.client as mqtt

        self.prefix = f"{settings.get('topic_prefix', 'sentinel')}/{site_id}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        username = os.environ.get("MQTT_USERNAME")
        if username:
            self.client.username_pw_set(username, os.environ.get("MQTT_PASSWORD"))
        if settings.get("tls", False):
            self.client.tls_set()
        self.client.will_set(f"{self.prefix}/status", "offline", qos=1, retain=True)
        self.client.connect_async(settings.get("host", "localhost"), int(settings.get("port", 1883)))
        self.client.loop_start()
        self.client.publish(f"{self.prefix}/status", "online", qos=1, retain=True)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        retain = topic in {"settings", "drone"}
        self.client.publish(f"{self.prefix}/{topic}", json.dumps(payload, default=str), qos=1, retain=retain)

    def on_bus(self, topic: str, payload: dict[str, Any]) -> None:
        if topic in {"event", "incident", "notification", "settings", "drone"}:
            self.publish(topic, payload)

    def close(self) -> None:
        self.client.publish(f"{self.prefix}/status", "offline", qos=1, retain=True)
        self.client.loop_stop()
        self.client.disconnect()
