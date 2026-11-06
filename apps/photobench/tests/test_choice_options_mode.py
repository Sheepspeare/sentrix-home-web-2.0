"""选择题「提供选项」模式：选项轮拼装、选择解析与确定性评分。"""

from __future__ import annotations

import copy
import json
import threading

import pytest

from photobench.application.run_execution.judge import JudgeRunner
from photobench.application.run_execution.profile_scoring import ProfileScoring
from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
from photobench.domain.evaluation.contract import scoring_options
from photobench.domain.evaluation.profiles import ATM, OPEN, profile_configs
from photobench.domain.runs.choice_options import choice_prompt, option_labels, parse_choice_answer

OPTIONS = [
    {"id": "A", "text": "江滩公园东门"},
    {"id": "B", "text": "江滩公园西门"},
    {"id": "C", "text": "沙湖路 18 号"},
]


def test_labels_append_other_when_absent():
    labels = option_labels(OPTIONS)
    assert [item["letter"] for item in labels] == ["A", "B", "C", "D"]
    assert labels[-1]["option_id"] == "other"


def test_labels_keep_native_other():
    labels = option_labels([*OPTIONS, {"id": "E", "text": "其他"}])
    assert len(labels) == 4
    assert [item["option_id"] for item in labels] == ["A", "B", "C", "E"]


def test_prompt_carries_options_and_keeps_question_out():
    text = choice_prompt(option_labels(OPTIONS))
    assert "A. 江滩公园东门" in text and "D. 其他（以上都不是）" in text
    assert '{"choice": "A", "reason": "一句话说明依据"}' in text


@pytest.mark.parametrize(
    "answer,expected",
    [
        ('{"choice": "B", "reason": "牌子写着西门"}', ("ok", "B")),
        ('说明如下 {"choice":"C"}', ("ok", "C")),
        ("choice: A", ("ok", "A")),
        ("B", ("ok", "B")),
        ("b", ("ok", "B")),
        ("C. 沙湖路 18 号", ("ok", "C")),
        ("答案：B", ("ok", "B")),
        ("选择 B", ("ok", "B")),
        ("(B)", ("ok", "B")),
        ("B或者C", ("failed", None)),
        ("B. 西门\nC. 沙湖路", ("failed", None)),
        ("choice: B choice: C", ("failed", None)),
        ("choice: B或者C", ("failed", None)),
        ("choice: B 或 C", ("failed", None)),
        ("B. 但也可能C", ("failed", None)),
        ("B. 江滩公园东门", ("failed", None)),
        ('{"choice": "B或C"}', ("failed", None)),
        ('{"choice": ["B"]}', ("failed", None)),
        ("我认为是 B", ("failed", None)),
        ("", ("failed", None)),
        ('{"choice": "Z", "reason": "越界"}', ("failed", None)),
    ],
)
def test_parse_choice_answer(answer, expected):
    labels = option_labels(OPTIONS)
    parsed = parse_choice_answer(answer, labels)
    assert parsed["status"] == expected[0]
    assert parsed.get("letter") == expected[1]


def test_choice_options_visible_defaults_true_and_validates():
    assert scoring_options()["choice_options_visible"] is True
    assert scoring_options({"choice_options_visible": False})["choice_options_visible"] is False
    with pytest.raises(ValueError):
        scoring_options({"choice_options_visible": "yes"})


def test_profile_config_defaults_visible_and_rejects_on_open():
    assert profile_configs([ATM])[ATM]["choice_options_visible"] is True
    hidden = profile_configs([ATM], {ATM: {"choice_options_visible": False}})
    assert hidden[ATM]["choice_options_visible"] is False
    with pytest.raises(ValueError):
        profile_configs([OPEN], {OPEN: {"choice_options_visible": False}})


def _scoring_state(answer: str, *, correct_option: str = "B"):
    row = {
        "qa_id": "q",
        "question": "哪个门？",
        "answer": "西门",
        "expected_action": "answer",
        "evaluation": {
            "scorer": "choice_judge",
            "answer_format": "choice",
            "max_score": 1,
            "applies_to_turn": 0,
            "options": OPTIONS,
            "correct_option": correct_option,
            "review_status": "verified",
        },
    }
    return {
        "run_id": "r",
        "album_id": "album-x",
        "status": "completed",
        "qa_count": 1,
        "qa_set": "frozen",
        "qa_snapshot": {"rows": [row], "definition_sha256": qa_fingerprint([row])},
        "items": [
            {
                "qa_id": "q",
                "question": "哪个门？",
                "execution_status": "completed",
                "runtime_turns": [{"message": "哪个门？", "answer": answer, "turn_id": "t"}],
                "answer": answer,
                "conversation_turn_count": 1,
            }
        ],
        "scoring_batches": [],
        "scoring_results": {},
    }


def _score(state, visible: bool):
    from photobench.application.run_execution.profile_scoring import prepare_batches
    from photobench.domain.evaluation.profiles import scoring_targets

    batches = prepare_batches(
        state,
        profile_configs([ATM], {ATM: {"choice_options_visible": visible}}),
        {"url": "http://judge/v1", "model": "j"},
    )

    class Http:
        """公共诊断仍会调用 Judge；这里只关心选择题本身不再发请求。"""

        def __init__(self):
            self.calls = []

        def request_json(self, url, payload=None, *args, **kwargs):
            self.calls.append({"url": url, "payload": payload})
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"actual_action": "answer", "reason": "作答", "accuracy": True},
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 11},
            }

    http = Http()
    runner = JudgeRunner(http=http, judge_url="http://judge/v1", judge_model="j", cancel=threading.Event())
    service = ProfileScoring(runner, state, threading.RLock(), lambda: None, {})
    return service.score(batches[0], scoring_targets(state, batches[0], "all")[0]), http


def test_visible_mode_scores_by_parsed_letter_without_judge():
    state = _scoring_state('{"choice": "B", "reason": "西门牌匾"}')
    item = state["items"][0]
    item["agent_choice"] = parse_choice_answer(item["answer"], option_labels(OPTIONS))
    result, http = _score(state, True)
    assert result["score"] == 1 and result["judge_status"] == "completed"
    assert result["matched_option"] == "B"
    assert result["answer_side"] == "agent_choice"
    assert result["reason"] == "西门牌匾"
    assert "正确选项" not in json.dumps(http.calls, ensure_ascii=False)


def test_visible_mode_wrong_choice_scores_zero():
    state = _scoring_state('{"choice": "A", "reason": "东门"}')
    item = state["items"][0]
    item["agent_choice"] = parse_choice_answer(item["answer"], option_labels(OPTIONS))
    result, _ = _score(state, True)
    assert result["score"] == 0 and result["judge_status"] == "completed"


def test_visible_mode_unparsable_is_failure_not_zero():
    state = _scoring_state("我认为大概是西门吧")
    item = state["items"][0]
    item["agent_choice"] = parse_choice_answer(item["answer"], option_labels(OPTIONS))
    result, _ = _score(state, True)
    assert result["score"] is None
    assert result["judge_status"] == "failed"
    assert "json解析失败" in result["reason"]


def test_blind_mode_still_uses_judge():
    state = _scoring_state('{"choice": "B", "reason": "x"}')
    item = state["items"][0]
    item["agent_choice"] = parse_choice_answer(item["answer"], option_labels(OPTIONS))
    result, http = _score(state, False)
    # 盲选项模式不读 agent_choice，仍按 Judge 语义匹配走。
    assert result.get("answer_side") != "agent_choice"
    assert any("比较" in json.dumps(c["payload"], ensure_ascii=False) for c in http.calls)


def test_judge_skips_appended_option_turn():
    """开放式标准只评选项轮之前的原题回答；选项轮不产生 task/evidence/judge。"""
    row = {
        "qa_id": "q",
        "question": "石碑刻了什么？",
        "answer": "三峡坝址基石",
        "expected_action": "answer",
        # 题面只有一轮：选项轮是测评服务追加的，超出 conversation 长度。
        "conversation": [{"message": "石碑刻了什么？", "expected_action": "answer"}],
        "evaluation": {
            "scorer": "choice_judge",
            "answer_format": "choice",
            "max_score": 1,
            "applies_to_turn": 0,
            "options": OPTIONS,
            "correct_option": "C",
            "review_status": "verified",
        },
    }
    item = {
        "qa_id": "q",
        "execution_status": "completed",
        "reference_answer": "三峡坝址基石",
        "runtime_turns": [
            {"message": "石碑刻了什么？", "answer": "刻的是坝址基石。", "expected_action": "answer"},
            {"message": "请整理前面的信息…", "answer": '{"choice":"C","reason":"照片原文"}'},
        ],
    }
    calls = []

    class Http:
        def request_json(self, url, payload=None, *a, **k):
            calls.append(payload)
            body = json.dumps({"actual_action": "answer", "reason": "作答"})
            return {"choices": [{"message": {"content": body}}], "usage": {"total_tokens": 5}}

    runner = JudgeRunner(http=Http(), judge_url="http://j/v1", judge_model="j", cancel=threading.Event())
    out = runner.judge_item(item, row, {})
    # 选项轮不发任何请求：唯一的调用是原题轮的任务判定。
    assert len(calls) == 1
    # 聚合只看原题轮，选项轮不进 task_judges。
    assert len(out["task_judges"]) == 1
    # 选择题不产生答题质量分，且不因此把整题判成评分失败。
    assert out["judge"].get("reason") in {"not_labeled", "not_applicable"}
    assert out["judge_status"] == "completed"


def test_non_open_scorer_never_requests_quality_judge():
    """选择题不走答题质量通道：整轮不发请求，也不要求 0/1/2 质量分。"""
    calls = []

    class Http:
        def request_json(self, url, payload=None, *a, **k):
            calls.append(payload)
            body = json.dumps({"actual_action": "answer", "reason": "作答"})
            return {"choices": [{"message": {"content": body}}]}

    runner = JudgeRunner(http=Http(), judge_url="http://j/v1", judge_model="j", cancel=threading.Event())
    row = {
        "evaluation": {
            "scorer": "choice_judge",
            "answer_format": "choice",
            "max_score": 1,
            "applies_to_turn": 0,
            "options": OPTIONS,
            "correct_option": "A",
            "review_status": "verified",
        }
    }
    item = {
        "qa_id": "q",
        "execution_status": "completed",
        "reference_answer": "ref",
        "runtime_turns": [{"message": "q", "answer": "a", "expected_action": "answer"}],
    }
    out = runner.judge_item(item, row, {})
    # 只有任务判定这一个请求，没有质量评分请求。
    assert len(calls) == 1
    assert out["judge"].get("reason") in {"not_labeled", "not_applicable"}
    assert out["judge_status"] == "completed"


def test_turn_count_uses_question_design_not_option_turn():
    """conversation_turn_count 按题面记，processed_turn_count 记实际处理轮数。"""
    from photobench.application.run_execution.service import turn_counts

    assert turn_counts(conversation=[{}, {}, {}], turn_records=[{}, {}, {}, {}]) == (3, 4)
    assert turn_counts(conversation=[], turn_records=[{}]) == (1, 1)


@pytest.mark.parametrize("authored", [1, 2])
@pytest.mark.parametrize("profiles", [[ATM], [OPEN, ATM]])
@pytest.mark.parametrize(
    "answer,expected_score",
    [
        ('{"choice":"B","reason":"西门"}', 1),
        ("A. 江滩公园东门", 0),
        ("江滩公园西门", None),
    ],
)
def test_choice_original_turns_shared_by_scoring_recovery_and_completion(
    authored, profiles, answer, expected_score
):
    from photobench.application.run_execution.profile_scoring import prepare_batches
    from photobench.domain.evaluation.profiles import (
        item_scoring_error,
        result_key,
        scoring_targets,
        summarize_batches,
    )

    state = _scoring_state("原始自由回答")
    row = state["qa_snapshot"]["rows"][0]
    row["evaluation"]["applies_to_turn"] = authored - 1
    if authored > 1:
        row["conversation"] = [
            {"message": f"原问题{index}", "reference_answer": "西门", "expected_action": "answer"}
            for index in range(authored)
        ]
    item = state["items"][0]
    item["conversation_turn_count"] = authored
    item["processed_turn_count"] = authored + 1
    item["choice_prompt"] = {"mode": "options_visible", "labels": option_labels(OPTIONS)}
    item["runtime_turns"] = [
        {"message": f"原问题{index}", "answer": "原始自由回答", "expected_action": "answer"}
        for index in range(authored)
    ] + [{"message": "提供选项", "answer": answer}]
    item["agent_choice"] = parse_choice_answer(answer, option_labels(OPTIONS))
    calls = []

    class Http:
        def request_json(self, url, payload=None, *args, **kwargs):
            calls.append(copy.deepcopy(payload))
            assert "提供选项" not in json.dumps(payload, ensure_ascii=False)
            assert answer not in json.dumps(payload, ensure_ascii=False)
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"score": 2, "accuracy": True, "actual_action": "answer", "reason": "ok"}
                            )
                        }
                    }
                ]
            }

    batches = prepare_batches(state, profile_configs(profiles), {"url": "http://judge/v1", "model": "j"})
    state["primary_scoring_batch_ids"] = [b["batch_id"] for b in batches]
    runner = JudgeRunner(http=Http(), judge_url="http://judge/v1", judge_model="j", cancel=threading.Event())
    service = ProfileScoring(runner, state, threading.RLock(), lambda: None, {})
    service.score_item(batches, item, row)
    assert len(state["scoring_results"]) == authored * len(profiles)
    atm = next(b for b in batches if b["profile_id"] == ATM)
    result = state["scoring_results"][result_key(atm["batch_id"], "q", authored - 1)]
    assert result["score"] == expected_score
    assert not result["requests"]
    if expected_score is not None:
        assert result["agent_choice"]["answer"] == answer
        assert item_scoring_error(state, item) is None
    else:
        assert "json解析失败" in item_scoring_error(state, item)
        assert summarize_batches(state)[atm["batch_id"]]["scored"] == 0
    for batch in batches:
        for scope in ["all", "missing", "failed", "unscored", "needs_review"]:
            assert all(t["turn_index"] < authored for t in scoring_targets(state, batch, scope))
    key = result_key(atm["batch_id"], "q", authored - 1)
    del state["scoring_results"][key]
    targets = scoring_targets(state, atm, "missing")
    assert any(t["key"] == key for t in targets)
    for target in targets:
        service.score(atm, target)
    assert state["scoring_results"][key]["score"] == expected_score


def test_original_turn_selection_preserves_unmarked_historical_dialogue():
    from photobench.domain.evaluation.profiles import scoring_turns

    turns = [{"answer": "one"}, {"answer": "two"}]
    assert scoring_turns({}, {"runtime_turns": turns, "conversation_turn_count": 1}) == turns
