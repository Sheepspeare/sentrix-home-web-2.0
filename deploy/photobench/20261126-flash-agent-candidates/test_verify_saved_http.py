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

    # Concurrent identical planner requests can have different recorded cache usage.
    for record, cached in zip(records, [0, 1024], strict=True):
        record["started_at"] = 1
        record["usage"]["prompt_tokens_details"] = {"cached_tokens": cached}
    log.write_text("\n".join(json.dumps(r) for r in records))
    config["expected"][0]["cached_tokens"] = 0
    assert m.verify(config)["requests"][0]["id"] == "0"

    # Matching cache usage still does not justify choosing an arbitrary archive.
    records[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 0
    log.write_text("\n".join(json.dumps(r) for r in records))
    with pytest.raises(AssertionError, match="extra archive"):
        m.verify(config)


def test_vision_prompt_disambiguates_same_usage():
    e = {"start": 0, "end": 2, "internal": True, "asked": "read label"}
    record = {"started_at": 1}
    m = module()
    assert m.matches(e, record, {"messages": [{"content": [{"text": "read label"}]}]}, {})
    assert not m.matches(e, record, {"messages": [{"content": [{"text": "identify food"}]}]}, {})


def test_saved_response_tool_ids_disambiguate_identical_concurrent_requests(tmp_path):
    records = []
    for index, ids in enumerate([["other_a", "other_b"], ["saved_b", "saved_a"]]):
        archive = tmp_path / f"{index}.gz"
        with gzip.open(archive, "wt") as stream:
            json.dump(
                {
                    "request": {
                        "model": "qwen3.8-flash",
                        "enable_thinking": False,
                        "temperature": 0,
                        "max_tokens": 4096,
                        "messages": [{"role": "user", "content": "question"}],
                    },
                    "response_body": json.dumps(
                        {"choices": [{"message": {"tool_calls": [{"id": i} for i in ids]}}]}
                    ),
                },
                stream,
            )
        records.append(
            {
                "id": str(index),
                "started_at": 1,
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 2048},
                },
                "finish_reasons": ["tool_calls"],
                "has_image": False,
                "http_status": 200,
                "archive": str(archive),
            }
        )
    log = tmp_path / "log"
    log.write_text("\n".join(json.dumps(r) for r in records))
    expected = {
        "id": "call",
        "qa_id": "q",
        "signature": [5, 2, "tool_calls", False],
        "internal": False,
        "start": 0,
        "end": 2,
        "cached_tokens": 2048,
        "response_tool_call_ids": ["saved_a", "saved_b"],
    }
    config = {
        "log": str(log),
        "start": 0,
        "end": 2,
        "questions": {"q": "question"},
        "expected": [expected],
    }
    m = module()
    assert m.verify(config)["requests"][0]["id"] == "1"

    # A partial set cannot identify the saved response.
    expected["response_tool_call_ids"] = ["saved_a"]
    with pytest.raises(AssertionError, match="call"):
        m.verify(config)

    # The same saved IDs in two archives remain ambiguous.
    expected["response_tool_call_ids"] = ["saved_a", "saved_b"]
    records.append({**records[1], "id": "duplicate"})
    log.write_text("\n".join(json.dumps(r) for r in records))
    with pytest.raises(AssertionError, match="extra archive"):
        m.verify(config)


@pytest.mark.parametrize(
    ("call_type", "section"),
    [
        ("faithfulness_judge", "\n完整对话轨迹（含全部工具返回与 captured_at 等元数据）：\n"),
        ("claim_verifier", "\n待核验 claim（time）："),
    ],
)
def test_verifier_requires_full_question_in_its_recorded_prompt_format(call_type, section):
    m = module()
    expected = {"start": 0, "end": 2, "internal": False, "qa_id": "q", "call_type": call_type}
    record = {"started_at": 1}
    questions = {"q": "Full original question?"}
    content = "用户问题：Full original question?" + section + "saved evidence"
    request = {"messages": [{"role": "user", "content": content}]}
    assert m.matches(expected, record, request, questions)
    assert not m.matches(expected, record, request, {"q": "original question?"})
    assert not m.matches({**expected, "call_type": "agent"}, record, request, questions)
    assert not m.matches(expected, record, {"messages": [{"role": "tool", "content": content}]}, questions)
    assert not m.matches(
        expected,
        record,
        {"messages": [{"role": "user", "content": "用户问题：Full original question?\nother context"}]},
        questions,
    )


def test_field_verifier_matches_exact_json_query_not_nested_fact_text():
    verifier = module()
    expected = {"start": 0, "end": 2, "internal": False, "call_type": "claim_verifier", "qa_id": "q"}
    record = {"started_at": 1}
    payload = {"query": "question", "answer": "answer", "requirements": [], "facts": []}
    request = {"messages": [{"role": "user", "content": json.dumps(payload)}]}
    assert verifier.matches(expected, record, request, {"q": "question"})
    payload.update(query="other question", facts=[{"value": "question"}])
    request["messages"][0]["content"] = json.dumps(payload)
    assert not verifier.matches(expected, record, request, {"q": "question"})


def test_condition_verifier_requires_exact_query_and_structured_updates_and_facts():
    verifier = module()
    expected = {"start": 0, "end": 2, "internal": False, "call_type": "claim_verifier", "qa_id": "q"}
    record = {"started_at": 1}
    payload = {"query": "question", "condition_updates": [{"original": {}, "update": {}}], "facts": []}

    def matches(value, role="user"):
        return verifier.matches(
            expected, record, {"messages": [{"role": role, "content": json.dumps(value)}]}, {"q": "question"}
        )

    assert matches(payload)
    assert not matches(payload, "tool")
    assert not matches({**payload, "query": "other", "facts": [{"value": "question"}]})
    assert not matches({**payload, "condition_updates": {}})
    assert not matches({**payload, "facts": "question"})


def test_timeout_request_is_preserved_without_claiming_success(tmp_path):
    archive = tmp_path / "timeout.gz"
    with gzip.open(archive, "wt") as stream:
        json.dump(
            {
                "request": {
                    "model": "qwen3.8-flash",
                    "enable_thinking": False,
                    "temperature": 0,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": "question"}],
                }
            },
            stream,
        )
    log = tmp_path / "requests.jsonl"
    log.write_text(
        json.dumps(
            {
                "id": "timeout",
                "started_at": 1,
                "has_image": False,
                "error_type": "ReadTimeout",
                "archive": str(archive),
            }
        )
    )
    result = module().verify(
        {
            "log": str(log),
            "start": 0,
            "end": 2,
            "questions": {"q": "question"},
            "expected": [
                {
                    "id": "attempt1",
                    "qa_id": "q",
                    "signature": [None, None, None, False],
                    "internal": False,
                    "start": 0,
                    "end": 2,
                }
            ],
        }
    )
    assert result["matched_calls"] == 1
    assert result["http_status_counts"] == {"None": 1}
    assert result["requests"][0]["error_type"] == "ReadTimeout"
