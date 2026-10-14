"""AI providers behind one small interface.

Conversations are kept in the Messages API shape (role + content blocks:
text, tool_use, tool_result), which is the canonical format here; another
provider implements `complete()` by translating to and from it.

The only built-in external provider is Anthropic's Claude, used through the
official `anthropic` SDK (an optional dependency). It is off unless the owner
turns it on (`assistant.provider: "anthropic"`), and its key comes from the
server (the `ANTHROPIC_API_KEY` environment variable or the credential store),
never from the browser. Any failure raises `ProviderError`, and the assistant
answers with the built-in guide instead."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5-5"
API_KEY_CREDENTIAL = "assistant.anthropic_api_key"


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class ProviderReply:
    content: list[dict[str, Any]]  # assistant content blocks, appended to the history unchanged
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str
    model: str
    external: bool  # sends data off this hub

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderReply: ...


def _echoable(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """After a mid-output fallback, blocks before the last `fallback` marker that
    are thinking or tool calls belong to the declined attempt and are not echoed."""
    boundary = max((i for i, block in enumerate(blocks) if block.get("type") == "fallback"), default=-1)
    kept = []
    for index, block in enumerate(blocks):
        if block.get("type") == "fallback":
            continue
        if index < boundary and block.get("type") in ("thinking", "redacted_thinking", "tool_use"):
            continue
        kept.append(block)
    return kept


class AnthropicProvider:
    name = "anthropic"
    external = True

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL, effort: str = "low", timeout_s: float = 60.0,
                 base_url: str | None = None, max_tokens: int = 16000) -> None:
        try:
            import anthropic
        except ImportError:
            raise ProviderError("the anthropic package is not installed (pip install anthropic)") from None
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1, base_url=base_url or None)

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderReply:
        anthropic = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},  # the stable system prompt and tools are cached
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # a declined request is re-run on Anthropic's recommended fallback model
            )
        except anthropic.AuthenticationError:
            raise ProviderError("the AI provider rejected the API key") from None
        except anthropic.RateLimitError:
            raise ProviderError("the AI provider is rate limiting requests") from None
        except anthropic.APITimeoutError:
            raise ProviderError("the AI provider did not answer in time") from None
        except anthropic.APIConnectionError:
            raise ProviderError("the AI provider could not be reached") from None
        except anthropic.APIStatusError as error:
            raise ProviderError(f"the AI provider returned an error ({error.status_code})") from None
        if response.stop_reason == "refusal":
            raise ProviderError("the AI provider declined this request")
        blocks = _echoable([block.to_dict() for block in response.content])
        calls = [ToolCall(block["id"], block["name"], block.get("input")) for block in blocks if block.get("type") == "tool_use"]
        text = "\n\n".join(block["text"] for block in blocks if block.get("type") == "text" and block.get("text"))
        usage = response.usage.to_dict() if getattr(response, "usage", None) is not None else {}
        return ProviderReply(blocks, text, calls, response.stop_reason or "end_turn", usage)


def build_provider(settings: dict[str, Any], secret: Any = lambda name: None) -> tuple[Provider | None, str]:
    """(provider, reason). None means the built-in guide answers, with `reason` saying why."""
    if settings.get("provider", "none") != "anthropic":
        return None, "No AI provider is turned on: Sentinel's built-in guide is answering, entirely on this hub."
    key = os.environ.get("ANTHROPIC_API_KEY") or secret(API_KEY_CREDENTIAL)
    if not key:
        return None, "The AI provider is turned on but has no API key (set ANTHROPIC_API_KEY or save a key in the assistant settings)."
    try:
        return AnthropicProvider(api_key=key, model=settings.get("model", DEFAULT_MODEL), effort=settings.get("effort", "low"),
                                 timeout_s=float(settings.get("timeout_s", 60)), base_url=settings.get("base_url")), ""
    except ProviderError as error:
        return None, f"The AI provider is not available: {error}."
