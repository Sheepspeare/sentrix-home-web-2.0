import gzip
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "judge_http", Path(__file__).with_name("verify_judge_http.py")
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def fixture(tmp_path):
    verifier = module()
    request = {"model": "qwen3.8-flash", "enable_thinking": False, "temperature": 0, "max_tokens": 2048}
    response = {
        "id": "response",
        "model": "qwen3.8-flash",
        "usage": {"total_tokens": 5},
        "choices": [{"message": {"content": '{"accuracy":true}'}}],
    }
    archive = tmp_path / "raw.gz"
    with gzip.open(archive, "wt") as stream:
        json.dump({"request": request, "response_status": 200, "response_body": json.dumps(response)}, stream)
    record = {
        "id": "raw",
        "response_id": "response",
        "started_at": 10,
        "archive": str(archive),
        "http_status": 200,
        "usage": response["usage"],
    }
    log = tmp_path / "requests.jsonl"
    log.write_text(json.dumps(record) + "\n")
    entry = {
        "response_id": "response",
        "start": 9,
        "end": 11,
        "group": "one",
        "qa_id": "q",
        "stage": "new",
        "request_sha256": verifier.digest(request),
        "response_sha256": verifier.digest(response),
        "usage": response["usage"],
    }
    return verifier, {"log": str(log), "expected": [entry]}, record


def test_exact_complete_payload_and_response_match(tmp_path):
    verifier, config, _ = fixture(tmp_path)
    result = verifier.verify(config)
    assert result["matched_calls"] == 1 and result["generated_model_requests"] == 0


def test_duplicate_raw_response_is_rejected(tmp_path):
    verifier, config, record = fixture(tmp_path)
    with Path(config["log"]).open("a") as stream:
        stream.write(json.dumps({**record, "id": "second"}) + "\n")
    with pytest.raises(AssertionError, match="Multiple archives"):
        verifier.verify(config)


def test_same_response_id_different_payload_is_rejected(tmp_path):
    verifier, config, _ = fixture(tmp_path)
    config["expected"][0]["request_sha256"] = verifier.digest({"other": "payload"})
    with pytest.raises(AssertionError, match="payload differs"):
        verifier.verify(config)


def invalid_json_fixture(tmp_path):
    verifier, config, record = fixture(tmp_path)
    archive = Path(record["archive"])
    with gzip.open(archive, "rt") as stream:
        raw = json.load(stream)
    response = json.loads(raw["response_body"])
    response["choices"][0]["message"]["content"] = "```json\n[]\n```"
    raw["response_body"] = json.dumps(response)
    with gzip.open(archive, "wt") as stream:
        json.dump(raw, stream)
    config["expected"][0]["response_sha256"] = verifier.digest(response)
    return verifier, config


def test_invalid_content_is_rejected_for_successful_score(tmp_path):
    verifier, config = invalid_json_fixture(tmp_path)
    with pytest.raises(AssertionError, match="JSON validity differs"):
        verifier.verify(config)


def test_explicit_invalid_content_evidence_still_requires_exact_raw(tmp_path):
    verifier, config = invalid_json_fixture(tmp_path)
    config["expected"][0]["content_json_valid"] = False
    result = verifier.verify(config)
    assert result["requests"][0]["content_json_valid"] is False
    config["expected"][0]["response_sha256"] = "modified"
    with pytest.raises(AssertionError, match="response differs"):
        verifier.verify(config)


def test_valid_content_cannot_be_reported_as_invalid(tmp_path):
    verifier, config, _ = fixture(tmp_path)
    config["expected"][0]["content_json_valid"] = False
    with pytest.raises(AssertionError, match="JSON validity differs"):
        verifier.verify(config)
