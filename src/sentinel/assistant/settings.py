"""The `assistant` config section and its editor.

    "assistant": {
      "provider": "none" | "anthropic",   # default "none": the built-in guide, nothing leaves the hub
      "model": "claude-opus-5-5",
      "effort": "low",                     # low | medium | high (chat rarely needs more)
      "share_network_details": false,      # true sends real private addresses to the provider
      "max_tool_rounds": 8,                # tool calls per customer message
      "timeout_s": 60
    }

The provider API key is never in the config: it comes from ANTHROPIC_API_KEY or
the credential store entry `assistant.anthropic_api_key` (write-only over the API)."""

from __future__ import annotations

import copy
import re
from typing import Any

from .provider import API_KEY_CREDENTIAL, DEFAULT_MODEL

PROVIDERS = ("none", "anthropic")
EFFORTS = ("low", "medium", "high")
_MODEL = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,60}$")


def validate_assistant(config: dict[str, Any], errors: list[str]) -> None:
    settings = config.get("assistant")
    if settings is None:
        return
    if not isinstance(settings, dict):
        errors.append("assistant must be an object")
        return
    if settings.get("provider", "none") not in PROVIDERS:
        errors.append(f"assistant.provider must be one of {list(PROVIDERS)}")
    if "model" in settings and not (isinstance(settings["model"], str) and _MODEL.match(settings["model"])):
        errors.append("assistant.model must be a model id such as claude-opus-5-5")
    if settings.get("effort", "low") not in EFFORTS:
        errors.append(f"assistant.effort must be one of {list(EFFORTS)}")
    if "share_network_details" in settings and not isinstance(settings["share_network_details"], bool):
        errors.append("assistant.share_network_details must be true or false")
    if "max_tool_rounds" in settings and not (isinstance(settings["max_tool_rounds"], int) and 1 <= settings["max_tool_rounds"] <= 20):
        errors.append("assistant.max_tool_rounds must be an integer from 1 to 20")
    if "timeout_s" in settings and not (isinstance(settings["timeout_s"], (int, float)) and 5 <= settings["timeout_s"] <= 300):
        errors.append("assistant.timeout_s must be between 5 and 300")
    if "base_url" in settings and not re.match(r"^https://[^\s@]+$", str(settings["base_url"])):
        errors.append("assistant.base_url must be an https URL without credentials")
    for key in settings:
        if "key" in key.lower() or "token" in key.lower() or "secret" in key.lower():
            errors.append(f"assistant.{key}: API keys are never stored in the config; use the credential store")


def save_settings(service: Any, payload: dict[str, Any], actor: str) -> None:
    """Validated through the normal config editor; an API key goes to the credential store."""
    site = service.site
    config = {key: value for key, value in copy.deepcopy(site.config).items() if not key.startswith("_")}
    settings = dict(config.get("assistant", {}))
    for key in ("provider", "model", "effort", "share_network_details"):
        if key in payload and payload[key] is not None:
            settings[key] = payload[key]
    settings.setdefault("model", DEFAULT_MODEL)
    config["assistant"] = settings
    api_key = str(payload.get("api_key") or "").strip()
    if api_key:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", api_key):
            raise ValueError("that does not look like an API key")
        service.runtime.secrets.set(API_KEY_CREDENTIAL, api_key)
    if payload.get("forget_api_key"):
        service.runtime.secrets.delete(API_KEY_CREDENTIAL)
    site.replace(config, actor, "assistant settings saved")
