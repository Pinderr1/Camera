"""HTTP routes for the assistant, under /api/v1/assistant. Same bearer-token /
cookie authentication as the rest of the API; responses never contain the
provider key, stored passwords or stream addresses."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .core import Assistant
from .settings import save_settings
from .store import ConversationNotFound


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DecisionBody(BaseModel):
    confirm: bool


class EventBody(BaseModel):
    event: Literal["channels_scanned", "cameras_added"]


class SettingsBody(BaseModel):
    provider: Literal["none", "anthropic"] | None = None
    model: str | None = Field(default=None, max_length=60)
    effort: Literal["low", "medium", "high"] | None = None
    share_network_details: bool | None = None
    api_key: str | None = Field(default=None, max_length=200)  # write-only: stored in the credential store, never returned
    forget_api_key: bool = False


def add_routes(app: FastAPI, service: Any, require_auth: Any) -> Assistant:
    from ..site_config import SiteConfigError

    assistant = Assistant(service)
    service.assistant = assistant

    def conversation_call(function, *args):
        try:
            return function(*args)
        except (ConversationNotFound, KeyError):
            raise HTTPException(status_code=404, detail="unknown conversation or proposal") from None
        except ValueError as error:
            raise HTTPException(status_code=409 if "proposal is" in str(error) else 422, detail=str(error)) from None

    @app.get("/api/v1/assistant")
    def assistant_status(_: str = Depends(require_auth)) -> dict[str, Any]:
        return assistant.status()

    @app.put("/api/v1/assistant/settings")
    def assistant_settings(body: SettingsBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            save_settings(service, body.model_dump(exclude_none=True), actor)
        except (SiteConfigError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        service.runtime.store.audit(actor, "assistant_settings_changed", {"provider": body.provider, "key_changed": bool(body.api_key or body.forget_api_key)})
        return assistant.status()

    @app.get("/api/v1/assistant/conversations")
    def list_conversations(_: str = Depends(require_auth)) -> list[dict[str, Any]]:
        return assistant.store.list()

    @app.post("/api/v1/assistant/conversations")
    def create_conversation(_: str = Depends(require_auth)) -> dict[str, Any]:
        return assistant.create()

    @app.get("/api/v1/assistant/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        return conversation_call(assistant.view, conversation_id)

    @app.delete("/api/v1/assistant/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, actor: str = Depends(require_auth)) -> dict[str, bool]:
        conversation_call(assistant.delete, conversation_id, actor)
        return {"ok": True}

    @app.post("/api/v1/assistant/conversations/{conversation_id}/messages")
    def send_message(conversation_id: str, body: MessageBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return conversation_call(assistant.send, conversation_id, body.text, actor)

    @app.post("/api/v1/assistant/conversations/{conversation_id}/actions/{action_id}")
    def decide(conversation_id: str, action_id: str, body: DecisionBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return conversation_call(assistant.decide, conversation_id, action_id, body.confirm, actor)

    @app.post("/api/v1/assistant/conversations/{conversation_id}/events")
    def event(conversation_id: str, body: EventBody, actor: str = Depends(require_auth)) -> dict[str, Any]:
        return conversation_call(assistant.record_event, conversation_id, body.event, actor)

    return assistant
