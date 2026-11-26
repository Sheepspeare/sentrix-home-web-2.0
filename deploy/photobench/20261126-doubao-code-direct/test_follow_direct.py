import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("follow_direct", Path(__file__).with_name("follow_direct.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def result(qa, diagnostic="completed"):
    score = {
        "qa_id": qa,
        "turn_index": 0,
        "status": "completed",
        "profile_id": "atm_qs_v1",
        "is_evaluation_turn": True,
        "diagnostic_key": qa,
        "normalized_score": 1,
    }
    return {"scoring_results": {qa: score}, "diagnostic_results": {qa: {"status": diagnostic}}}


def test_previous_success_is_reused_and_conflict_rejected():
    scores = {}
    state = result("old")
    module.merge_scores(scores, state)
    module.merge_scores(scores, state)
    assert module.missing_ids(["old", "new", "not_ready"], {"old", "new"}, scores) == ["new"]
    changed = result("old")
    changed["scoring_results"]["old"]["normalized_score"] = 0
    with pytest.raises(AssertionError):
        module.merge_scores(scores, changed)


def test_failed_diagnostic_is_missing_not_a_zero_score():
    scores = {}
    module.merge_scores(scores, result("q", "failed"))
    assert scores == {}
    assert module.missing_ids(["q"], {"q"}, scores) == ["q"]


def test_hydration_uses_all_pages_and_fixed_revision(monkeypatch):
    item = {
        "qa_id": "q",
        "index": 4,
        "trace_storage": {"revision": 7},
        "answer": "preview",
        "execution_status": "completed",
        "runtime_turns": [],
        "content_fields": [{"path": "answer", "characters": 11}],
    }
    lookup = SimpleNamespace(detail_by_qa=lambda *args: (item, []))
    calls = []

    def get(port, path):
        calls.append(path)
        assert "revision=7" in path
        if "path=qa_id" in path:
            return {"read_revision": 7, "text": '"q"', "next_offset": None}
        if "offset=0&" in path:
            return {"read_revision": 7, "text": '"hello ', "next_offset": 7}
        return {"read_revision": 7, "text": 'world"', "next_offset": None}

    monkeypatch.setattr(module, "get", get)
    hydrated, receipt = module.hydrate(8885, "run", "q", lookup)
    assert hydrated["answer"] == "hello world" and item["answer"] == "preview"
    assert len(receipt["content_pages"]["answer"]) == 2 and len(calls) == 3
    monkeypatch.setattr(module, "get", lambda *_args: {"read_revision": 8})
    with pytest.raises(AssertionError):
        module.hydrate(8885, "run", "q", lookup)


def test_saved_result_list_projection_has_no_execution_status():
    listing = {"items": [{"qa_id": "q", "index": 3, "agent_status": "failed"}]}
    assert set(module.listed_answers(listing)) == {"q"}
    assert module.missing_ids(["q"], module.listed_answers(listing), {}) == ["q"]
