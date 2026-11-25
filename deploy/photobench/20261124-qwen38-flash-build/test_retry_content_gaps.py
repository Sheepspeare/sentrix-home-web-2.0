"""Targeted prompts must preserve source evidence and retain required summary fields."""

import json

import httpx
import pytest
import repair_content as repair
from retry_content_gaps import messages_for_retry


def test_summary_retry_preserves_input_and_removes_named_examples():
    evidence = {"role": "user", "content": '{"participants":[],"observations":[{"caption":"女子遛狗"}]}'}
    messages = messages_for_retry([{"role": "system", "content": "旧示例王建国"}, evidence], visual=False)
    assert messages[1] is evidence
    assert "王建国" not in messages[0]["content"]
    assert "participants" in messages[0]["content"]


def test_visual_retry_preserves_image_bytes_and_original_prompt():
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,unchanged"}}
    original = [{"role": "user", "content": [{"type": "text", "text": "原观察要求"}, image]}]
    updated = messages_for_retry(original, visual=True)
    assert updated[0]["content"][1] is image
    assert updated[0]["content"][0]["text"].startswith("原观察要求")
    assert original[0]["content"][0]["text"] == "原观察要求"


def test_json_response_mode_does_not_bypass_summary_validation(tmp_path, monkeypatch):
    sent = []

    def post(*args, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://test"),
            json={
                "model": repair.MODEL,
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"activity": "遛狗"})}}
                ],
            },
        )

    monkeypatch.setattr(repair.httpx, "post", post)
    with pytest.raises(repair.ModelOutputError, match="Missing summary or title"):
        repair.completion(tmp_path, [{"role": "user", "content": "观察"}], response_json=True)
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert sent[0]["max_tokens"] == 4096 and sent[0]["enable_thinking"] is False
    assert not (tmp_path / "accepted-response.json").exists()
