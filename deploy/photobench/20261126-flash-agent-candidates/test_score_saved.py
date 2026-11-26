import copy
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from photobench.application.run_execution.judge import JudgeRunner
from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
from photobench.domain.evaluation.profiles import profile_configs


def fixture():
    spec = importlib.util.spec_from_file_location("saved_scorer", Path(__file__).with_name("score_saved.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [
        {
            "qa_id": str(i),
            "question": "多少？",
            "answer": "2",
            "evaluation": {"scorer": "atm_open", "source_qtype": "open_end", "max_score": 1},
        }
        for i in range(2)
    ]
    source = {
        "run_id": "original",
        "qa_snapshot": {"rows": rows, "definition_sha256": qa_fingerprint(rows)},
        "items": [
            {
                "qa_id": row["qa_id"],
                "question": row["question"],
                "answer": "2",
                "execution_status": "completed",
            }
            for row in rows
        ],
    }
    protocol = {
        "provider_id": "j",
        "url": "http://judge/v1",
        "model": "judge",
        "judge_use_gt_images": False,
        "scoring_configs": profile_configs(["atm_qs_v1"]),
    }
    return module, source, protocol


class Http:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def request_json(self, url, payload, *args):
        self.calls.append(copy.deepcopy(payload))
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return {"choices": [{"message": {"content": json.dumps(value)}}]}


def runner(replies):
    http = Http(replies)
    return JudgeRunner(
        http=http, judge_url="http://judge/v1", judge_model="judge", cancel=threading.Event()
    ), http


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr("photobench.application.run_execution.judge.JUDGE_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("photobench.application.run_execution.judge.JUDGE_RETRY_ATTEMPTS", 1)


def test_frozen_scoring_preserves_inputs_and_does_not_repeat_success(tmp_path):
    op, source, protocol = fixture()
    before = copy.deepcopy(source)
    r, http = runner([{"accuracy": True}] * 2)
    assert op.score_saved(source, protocol, tmp_path, r)["status"] == "completed"
    assert len(http.calls) == 2 and source == before
    r, http = runner([])
    assert op.score_saved(source, protocol, tmp_path, r)["scored_this_invocation"] == 0
    assert not http.calls
    assert all(
        m["content"] == protocol["scoring_configs"]["atm_qs_v1"]["prompts"]["open"]
        for p in json.loads((tmp_path / "scoring-state.json").read_text())["scoring_results"].values()
        for q in p["requests"]
        for m in q["input"]["messages"]
        if m["role"] == "system"
    )


def test_first_judge_failure_stops_before_other_answers(tmp_path):
    op, source, protocol = fixture()
    r, http = runner([RuntimeError("HTTP 429 quota exhausted"), RuntimeError("HTTP 429 quota exhausted")])
    result = op.score_saved(source, protocol, tmp_path, r)
    assert result["status"] == "needs_review" and result["qa_id"] == "0"
    assert len(http.calls) == 1
    state = json.loads((tmp_path / "scoring-state.json").read_text())
    assert len(state["scoring_results"]) == 1


def test_changed_frozen_answer_is_rejected_on_resume(tmp_path):
    op, source, protocol = fixture()
    r, _ = runner([{"accuracy": True}] * 2)
    op.score_saved(source, protocol, tmp_path, r)
    source["items"][0]["answer"] = "changed"
    with pytest.raises(AssertionError):
        op.score_saved(source, protocol, tmp_path, r)


def four_answers():
    op, source, protocol = fixture()
    for index in range(2, 4):
        row = copy.deepcopy(source["qa_snapshot"]["rows"][0])
        row["qa_id"] = str(index)
        source["qa_snapshot"]["rows"].append(row)
        source["items"].append({**copy.deepcopy(source["items"][0]), "qa_id": str(index)})
    source["qa_snapshot"]["definition_sha256"] = qa_fingerprint(source["qa_snapshot"]["rows"])
    return op, source, protocol


def test_two_workers_score_simultaneously_and_resume_skips_success(tmp_path):
    op, source, protocol = four_answers()

    class ParallelHttp:
        def __init__(self):
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.active = self.maximum = self.calls = 0

        def request_json(self, url, payload, *args):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.calls += 1
            try:
                self.barrier.wait(timeout=5)
                return {"choices": [{"message": {"content": '{"accuracy":true}'}}]}
            finally:
                with self.lock:
                    self.active -= 1

    http = ParallelHttp()
    r = JudgeRunner(http=http, judge_url="http://judge/v1", judge_model="judge", cancel=threading.Event())
    assert op.score_saved(source, protocol, tmp_path, r, concurrency=2)["status"] == "completed"
    assert http.calls == 4 and http.maximum == 2
    r, http = runner([])
    assert op.score_saved(source, protocol, tmp_path, r, concurrency=2)["scored_this_invocation"] == 0
    assert not http.calls


def test_parallel_failure_keeps_inflight_success_and_stops_new_dispatch(tmp_path):
    op, source, protocol = four_answers()

    class FailureHttp:
        def __init__(self):
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.calls = 0

        def request_json(self, url, payload, *args):
            with self.lock:
                self.calls += 1
                index = self.calls
            self.barrier.wait(timeout=5)
            if index == 1:
                raise RuntimeError("judge failure")
            return {"choices": [{"message": {"content": '{"accuracy":true}'}}]}

    http = FailureHttp()
    r = JudgeRunner(http=http, judge_url="http://judge/v1", judge_model="judge", cancel=threading.Event())
    # Hold the successful result until the failure has reached its saved checkpoint.
    original = op.ProfileScoring.score
    failed_saved = threading.Event()

    def score(service, batch, target):
        result = original(service, batch, target)
        if result["status"] == "failed":
            failed_saved.set()
        else:
            assert failed_saved.wait(timeout=5)
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(op.ProfileScoring, "score", score)
        result = op.score_saved(source, protocol, tmp_path, r, concurrency=2)
    assert result["status"] == "needs_review" and result["scored_this_invocation"] == 2
    assert http.calls == 2
    saved = json.loads((tmp_path / "scoring-state.json").read_text())
    assert sorted(v["status"] for v in saved["scoring_results"].values()) == ["completed", "failed"]
