"""Interactive HTTP request validation and application dispatch."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_api_routes import client as client


def test_chat_http_contract_and_gt_source(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    calls = []
    service = client.app.state.container.chat

    def create(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"session_id": "chat-one", "scope_id": payload["scope_id"], "status": "preparing", "turns": []}

    monkeypatch.setattr(service, "create_chat", create)
    response = client.post(
        "/api/v1/chat/sessions",
        json={
            "scope_id": "space",
            "album_id": "album",
            "qa_set": "qa",
            "qa_id": "one",
            "model": {"base_url": "http://model/v1"},
        },
    )
    assert response.status_code == 202
    assert response.json()["session_id"] == "chat-one"
    assert calls[0]["qa_id"] == "one" and "answer" not in calls[0]
    assert client.post("/api/v1/chat/sessions", json={}).status_code == 422
    assert client.post("/api/v1/chat/sessions/chat-one/turns", json={"message": ""}).status_code == 422


def test_retest_http_passes_model_and_batch(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    calls = []

    def start(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((run_id, payload))
        return {"run_id": "single", "retest_context": {"source_run_id": run_id}}

    monkeypatch.setattr(client.app.state.container.run_suites, "start_retest", start)
    response = client.post(
        "/api/v1/runs/original/retest",
        json={"qa_id": "one", "scoring_batch": "batch", "model": {"profile": "other"}},
    )
    assert response.status_code == 202
    assert calls[0][0] == "original" and calls[0][1]["model"]["profile"] == "other"
    assert calls[0][1]["scoring_batch"] == "batch"


@pytest.mark.parametrize(
    "path,inputs",
    [
        ("/api/v1/chat/sessions", {"scope_id": "space"}),
        ("/api/v1/chat/sessions/chat-one/turns", {"message": "hello"}),
        ("/api/v1/runs/original/retest", {"qa_id": "one"}),
    ],
)
def test_reasoning_http_rejects_non_boolean(client: TestClient, path, inputs) -> None:
    assert client.post(path, json={**inputs, "reasoning": "false"}).status_code == 422


def test_reasoning_http_default_and_explicit_false(monkeypatch, client: TestClient) -> None:
    calls = []

    def create(payload):
        calls.append(payload)
        return {"session_id": "chat-one", "scope_id": "space", "status": "ready", "turns": []}

    monkeypatch.setattr(client.app.state.container.chat, "create_chat", create)
    assert (
        client.post("/api/v1/chat/sessions", json={"scope_id": "space", "reasoning": False}).status_code
        == 202
    )
    assert calls[-1]["reasoning"] is False
    spec = client.app.openapi()
    assert spec["components"]["schemas"]["ChatCreateRequest"]["properties"]["reasoning"]["default"] is True
    assert spec["components"]["schemas"]["RetestRequest"]["properties"]["reasoning"]["default"] is True
    assert (
        spec["paths"]["/api/v1/runs"]["post"]["requestBody"]["content"]["application/json"]["schema"][
            "properties"
        ]["reasoning"]["default"]
        is True
    )
def test_retest_progress_http_dispatch(monkeypatch, client: TestClient) -> None:
    progress = {
        "run_id": "single",
        "status": "running",
        "item": {"runtime_turns": [{"trace": [{"text": "event"}]}]},
    }
    monkeypatch.setattr(client.app.state.container.run_suites, "get_retest_progress", lambda run_id: progress)
    response = client.get("/api/v1/runs/single/progress")
    assert response.status_code == 200
    assert response.json() == progress
