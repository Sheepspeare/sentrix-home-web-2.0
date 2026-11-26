import gzip
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "saved_http", Path(__file__).with_name("verify_saved_http.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_same_signature_and_question_from_other_run_is_excluded(tmp_path):
    records = []
    for index, at in enumerate([1, 10]):
        archive = tmp_path / f"{index}.gz"
        with gzip.open(archive, "wt") as f:
            json.dump(
                {
                    "request": {
                        "model": "qwen3.8-flash",
                        "enable_thinking": False,
                        "temperature": 0,
                        "max_tokens": 4096,
                        "messages": [
                            {"role": "user", "content": "question"},
                            {"role": "tool", "content": "recorded evidence"},
                            {"role": "user", "content": "取证请求额度已用完"},
                        ],
                        "tools": [{"function": {"name": "final_answer"}}],
                    },
                    "response_body": json.dumps(
                        {"choices": [{"message": {"tool_calls": [{"function": {"name": "final_answer"}}]}}]}
                    ),
                },
                f,
            )
        records.append(
            {
                "id": str(index),
                "started_at": at,
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                "finish_reasons": ["stop"],
                "has_image": False,
                "http_status": 200,
                "archive": str(archive),
            }
        )
    log = tmp_path / "log"
    log.write_text("\n".join(json.dumps(r) for r in records))
    config = {
        "log": str(log),
        "start": 0,
        "end": 20,
        "questions": {"q": "question"},
        "expected": [
            {
                "id": "call",
                "qa_id": "q",
                "signature": [5, 2, "stop", False],
                "internal": False,
                "start": 9,
                "end": 11,
            }
        ],
    }
    m = module()
    result = m.verify(config)
    assert result["requests"][0]["id"] == "1"
    final = result["budget_finalization"][0]
    assert final["prior_tool_messages"] == 1
    assert final["tool_names"] == final["reply_tool_names"] == ["final_answer"]
    config["expected"][0]["start"] = 0
    with pytest.raises(AssertionError, match="extra archive"):
        m.verify(config)


def test_vision_prompt_disambiguates_same_usage():
    e = {"start": 0, "end": 2, "internal": True, "asked": "read label"}
    record = {"started_at": 1}
    m = module()
    assert m.matches(e, record, {"messages": [{"content": [{"text": "read label"}]}]}, {})
    assert not m.matches(e, record, {"messages": [{"content": [{"text": "identify food"}]}]}, {})
