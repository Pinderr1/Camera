"""DeepSeek provider, over DeepSeek's own OpenAI-format Chat Completions API
(https://api.deepseek.com/chat/completions, documented at api-docs.deepseek.com).

Conversations are kept in Sentinel's canonical block format (see provider.py);
this module translates both ways on every request:

- system prompt -> {"role": "system"}; text blocks -> user/assistant `content`;
- tool definitions -> {"type": "function", "function": {name, description, parameters}};
- assistant tool_use blocks -> `tool_calls` (arguments as a JSON string);
- tool_result blocks -> one {"role": "tool", "tool_call_id"} message each,
  placed directly after the assistant message that called them;
- thinking: DeepSeek returns `reasoning_content`. When a request carries `tools`,
  the API requires every earlier assistant message's `reasoning_content` to be
  sent back (a 400 otherwise), so it is stored as a `reasoning` block and
  replayed unchanged.

No Anthropic-specific parameters are sent. The key is only ever placed in the
Authorization header and never logged."""

from __future__ import annotations

import json
import time
from typing import Any

from .provider import ProviderError, ProviderReply, ToolCall

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"  # DeepSeek-V4.1-Flash
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEEPSEEK_KEY_CREDENTIAL = "assistant.deepseek_api_key"
# Sentinel's effort setting -> DeepSeek reasoning_effort (none, low, high, max).
EFFORT = {"low": "low", "medium": "high", "high": "high"}
_ERRORS = {
    400: "DeepSeek rejected the request format", 401: "DeepSeek rejected the API key", 402: "the DeepSeek account has no remaining balance",
    422: "DeepSeek rejected a request parameter", 429: "DeepSeek is rate limiting requests", 500: "DeepSeek had a server error",
    503: "DeepSeek is overloaded",
}


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if message["role"] == "assistant":
            text = "\n\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))
            reasoning = "".join(b.get("text", "") for b in blocks if b.get("type") == "reasoning")
            calls = [{"id": b["id"], "type": "function", "function": {
                "name": b["name"], "arguments": b["input"] if isinstance(b["input"], str) else json.dumps(b["input"])}}
                for b in blocks if b.get("type") == "tool_use"]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None, "reasoning_content": reasoning}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        # user turn: tool results first (they answer the previous assistant message), then any text
        for block in blocks:
            if block.get("type") == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = "\n".join(part.get("text", "") for part in result if isinstance(part, dict))
                out.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                            "content": ("ERROR: " if block.get("is_error") else "") + str(result)})
        text = "\n\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))
        if text:
            out.append({"role": "user", "content": text})
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
            for t in tools]


def from_openai_message(message: dict[str, Any]) -> tuple[list[dict[str, Any]], str, list[ToolCall]]:
    """(canonical assistant blocks, text, tool calls). Arguments that are not valid
    JSON are kept as the raw string: the tool layer rejects them with an error the
    model sees, and the history replays exactly what the model sent."""
    blocks: list[dict[str, Any]] = []
    if message.get("reasoning_content"):
        blocks.append({"type": "reasoning", "text": message["reasoning_content"]})
    text = message.get("content") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            arguments: Any = json.loads(raw)
        except (TypeError, ValueError):
            arguments = raw
        blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{len(calls)}", "name": function.get("name", ""), "input": arguments})
        calls.append(ToolCall(blocks[-1]["id"], blocks[-1]["name"], arguments))
    return blocks, text, calls


class DeepSeekProvider:
    name = "deepseek"
    display_name = "DeepSeek"
    external = True

    def __init__(self, *, api_key: str, model: str = DEEPSEEK_DEFAULT_MODEL, effort: str = "low", timeout_s: float = 60.0,
                 base_url: str | None = None, max_tokens: int = 16000, transport: Any = None) -> None:
        import httpx

        self._httpx = httpx
        self.model = model
        self.effort = EFFORT.get(effort, "low")
        self.max_tokens = max_tokens
        self.url = (base_url or DEEPSEEK_BASE_URL).rstrip("/") + "/chat/completions"
        self._client = httpx.Client(timeout=timeout_s, transport=transport,
                                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, messages),
            "thinking": {"type": "enabled"},
            "reasoning_effort": self.effort,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
            body["tool_choice"] = "auto"  # "required" and named choices are rejected in thinking mode
        response = self._post(body)
        try:
            choice = response["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("DeepSeek returned an unexpected response") from None
        finish = choice.get("finish_reason") or "stop"
        if finish == "content_filter":
            raise ProviderError("DeepSeek declined this request")
        if finish in ("insufficient_system_resource", "aborted"):
            raise ProviderError("DeepSeek could not finish the answer")
        blocks, text, calls = from_openai_message(message)
        stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")
        return ProviderReply(blocks, text, calls, stop, response.get("usage") or {})

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        httpx = self._httpx
        for attempt in (1, 2):
            try:
                response = self._client.post(self.url, json=body)
            except httpx.TimeoutException:
                raise ProviderError("DeepSeek did not answer in time") from None
            except httpx.HTTPError:
                if attempt == 1:
                    time.sleep(1.0)
                    continue
                raise ProviderError("DeepSeek could not be reached") from None
            if response.status_code in (500, 503) and attempt == 1:
                time.sleep(1.5)
                continue
            if response.status_code != 200:
                raise ProviderError(_ERRORS.get(response.status_code, f"DeepSeek returned an error ({response.status_code})"))
            try:
                return response.json()
            except ValueError:
                raise ProviderError("DeepSeek returned an unreadable response") from None
        raise ProviderError("DeepSeek could not be reached")
