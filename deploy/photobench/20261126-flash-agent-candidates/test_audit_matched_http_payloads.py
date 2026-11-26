import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "payload_audit", Path(__file__).with_name("audit_matched_http_payloads.py")
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def record(tmp_path, status=200, reasoning="", image="data:image/png;base64,aW1hZ2U="):
    path = tmp_path / "raw.gz"
    response = {
        "id": "response",
        "model": "qwen3.8-flash",
        "choices": [{"message": {"reasoning_content": reasoning}}],
        "usage": {"total_tokens": 12},
    }
    raw = {
        "request": {
            "model": "qwen3.8-flash",
            "enable_thinking": False,
            "temperature": 0,
            "max_tokens": 4096,
            "messages": [{"content": [{"type": "image_url", "image_url": {"url": image}}]}],
        },
        "response_status": status,
        "response_body": json.dumps(response) if status == 200 else '{"error":"bad image"}',
    }
    with gzip.open(path, "wt") as stream:
        json.dump(raw, stream)
    return {
        "call_id": "call",
        "id": "archive",
        "qa_id": "qa",
        "archive": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "http_status": status,
        "has_image": True,
        "response_id": "response",
        "usage": response["usage"] if status == 200 else None,
    }


def test_failed_attempt_preserves_unknown_usage_and_image_evidence(tmp_path):
    result = module().audit([record(tmp_path, status=400)])
    assert result["usage_missing_calls"] == ["call"]
    assert result["requests"][0]["usage"] is None
    assert result["image_payloads"] == 1
    assert result["http_status_counts"] == {"400": 1}


def test_success_checks_usage_and_rejects_reasoning_or_changed_archive(tmp_path):
    value = record(tmp_path)
    assert module().audit([value])["known_total_tokens"] == 12
    with pytest.raises(AssertionError):
        module().audit([record(tmp_path, reasoning="hidden reasoning")])
    value = record(tmp_path)
    Path(value["archive"]).write_bytes(b"changed")
    with pytest.raises(AssertionError, match="archive changed"):
        module().audit([value])


def test_empty_image_and_reused_call_are_rejected(tmp_path):
    with pytest.raises(AssertionError, match="Empty image"):
        module().audit([record(tmp_path, image="data:image/png;base64,")])
    value = record(tmp_path)
    with pytest.raises(AssertionError, match="Call reused"):
        module().audit([value, value])
