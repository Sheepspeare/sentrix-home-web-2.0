"""Additive interactive API contracts; raw product traces retain their existing fields."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelSelection(BaseModel):
    profile: str = "__current__"
    target_id: str = ""
    base_url: str = ""
    manager_url: str = ""
    endpoint_model: str = ""


class RetestRequest(BaseModel):
    qa_id: str = Field(min_length=1)
    model: ModelSelection | None = None
    scoring_profile: str = ""
    scoring_batch: str = ""


class RetestResponse(BaseModel):
    run_id: str
    retest_context: dict[str, Any]


class ChatCreateRequest(BaseModel):
    scope_id: str = Field(min_length=1)
    model: ModelSelection | None = None
    message: str = Field(default="", max_length=2000)
    album_id: str = ""
    qa_set: str = ""
    qa_id: str = ""


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatSessionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    session_id: str
    scope_id: str
    status: str
    source: dict[str, Any] | None = None
    turns: list[dict[str, Any]]
    items: list[dict[str, Any]] | None = None
