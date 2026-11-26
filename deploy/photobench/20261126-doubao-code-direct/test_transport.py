import importlib.util
import json
from pathlib import Path

import httpx


def module():
    spec = importlib.util.spec_from_file_location(
        "direct_transport", Path(__file__).with_name("transport.py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_native_body_preserves_tools_and_images():
    op = module()
    body = {
        "model": "qwen3.8-flash",
        "messages": [{"content": [{"image_url": {"url": "data:abc"}}]}],
        "tools": [{"function": {"name": "search"}}],
        "chat_template_kwargs": {"enable_thinking": True},
        "id_slot": 7,
        "max_tokens": 2048,
    }
    result = op.native_body(body)
    assert result["enable_thinking"] is False
    assert result["messages"] is body["messages"] and result["tools"] is body["tools"]
    assert result["max_tokens"] == 2048
    assert "chat_template_kwargs" not in result and "id_slot" not in result
    assert "enable_thinking" not in body


def test_direct_transport_records_actual_requests_without_touching_other_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASH_DIRECT_URL", "https://cloud.example/v1")
    monkeypatch.setenv("QWEN_API_KEY", "private-test")
    monkeypatch.setenv("FLASH_DIRECT_HTTP_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    op = module()
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "resp-1", "usage": {"total_tokens": 3}, "choices": []})

    op.install()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        client.post(
            "https://cloud.example/v1/chat/completions", json={"model": "qwen3.8-flash", "messages": []}
        )
        client.get("http://127.0.0.1:8300/health")
    assert seen[0].headers["authorization"] == "Bearer private-test"
    assert json.loads(seen[0].content)["enable_thinking"] is False
    assert "authorization" not in seen[1].headers
    assert len(list((tmp_path / "raw").glob("*.gz"))) == 1
    log = (tmp_path / "requests.jsonl").read_text()
    assert "private-test" not in log
    assert json.loads(log)["usage"]["total_tokens"] == 3
