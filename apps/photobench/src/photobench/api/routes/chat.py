"""Chat session transport; asynchronous work is owned by the application service."""

from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from photobench.api.deps import get_container
from photobench.api.interactive_schema import ChatCreateRequest, ChatMessageRequest, ChatSessionResponse

router = APIRouter(tags=["chat"])


@router.get("/chat/sessions")
def list_chats(request: Request) -> dict[str, Any]:
    return get_container(request).chat.list_chats()


@router.post(
    "/chat/sessions", status_code=202, response_model=ChatSessionResponse, response_model_exclude_unset=True
)
async def create_chat(request: Request, payload: ChatCreateRequest) -> dict[str, Any]:
    return await run_in_threadpool(
        get_container(request).chat.create_chat, payload.model_dump(exclude_defaults=True)
    )


@router.get(
    "/chat/sessions/{session_id}", response_model=ChatSessionResponse, response_model_exclude_unset=True
)
def get_chat(request: Request, session_id: str) -> dict[str, Any]:
    return get_container(request).chat.get_chat(session_id)


@router.post(
    "/chat/sessions/{session_id}/turns",
    status_code=202,
    response_model=ChatSessionResponse,
    response_model_exclude_unset=True,
)
async def send_chat(request: Request, session_id: str, payload: ChatMessageRequest) -> dict[str, Any]:
    return await run_in_threadpool(get_container(request).chat.send_chat, session_id, payload.model_dump())


@router.post("/chat/sessions/{session_id}/cancel")
def cancel_chat(request: Request, session_id: str) -> dict[str, Any]:
    return get_container(request).chat.cancel_chat(session_id)
