from __future__ import annotations

from collections.abc import Sequence

from sentrix.application.assistant.claim_verifier import ClaimVerifier
from sentrix.domain.assistant.claim_verifier import (
    Claim,
    ClaimVerdict,
    build_claim_messages,
    claim_numbers,
    high_risk_claims,
    parse_claim_review,
)
from sentrix.domain.assistant.evidence_binding import Fact
from sentrix.domain.assistant.evidence_ledger import RequirementValueType
from sentrix.domain.assistant.models import ChatMessage, ModelReply
from sentrix.domain.retrieval.query_conditions import QueryCondition


def test_high_risk_claims_extract_amount_time_count_and_place():
    claims = high_risk_claims(
        "婚宴一共花了 3000 元。那天是 2017 年 10 月。一共拍了 12 张照片。婚礼在河北省石家庄市举办。"
    )

    assert [claim.value_type for claim in claims] == [
        RequirementValueType.AMOUNT,
        RequirementValueType.TIME,
        RequirementValueType.COUNT,
        RequirementValueType.PLACE,
    ]


def test_claim_numbers_reads_arabic_and_chinese():
    assert claim_numbers("一共 12 张，三个人") == (12.0, 3.0)
    assert claim_numbers("十二张照片") == (12.0,)
    assert claim_numbers("两张") == (2.0,)


def test_a_plain_sentence_has_no_claims():
    assert high_risk_claims("照片里看起来很开心。") == ()


def test_parse_claim_review_reads_the_first_json_object():
    verdict, reason = parse_claim_review('{"verdict":"contradicted","reason":"工具是 5，回答写 3"} trailing')
    assert verdict is ClaimVerdict.CONTRADICTED
    assert reason == "工具是 5，回答写 3"


def test_parse_claim_review_fails_open():
    assert parse_claim_review("不是 JSON")[0] is ClaimVerdict.AMBIGUOUS


def test_claim_messages_only_include_matching_fact_types():
    claim = high_risk_claims("婚宴一共花了 3000 元。")[0]
    facts = [
        Fact("asset_amount", RequirementValueType.AMOUNT, 3000, provenance="a"),
        Fact("asset_count", RequirementValueType.COUNT, 5, provenance="b"),
    ]

    messages = build_claim_messages(query="婚宴花了多少钱", claim=claim, facts=facts, requirement=None)

    assert "asset_amount" in messages[1]["content"]
    assert "asset_count" not in messages[1]["content"]


class _ScriptedModel:
    def __init__(self, content: str | Exception) -> None:
        self.content = content
        self.calls: list[Sequence[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[object]) -> ModelReply:
        self.calls.append(messages)
        if isinstance(self.content, Exception):
            raise self.content
        return ModelReply(content=self.content, finish_reason="stop")


def test_claim_verifier_parses_a_contradiction():
    model = _ScriptedModel('{"verdict":"contradicted","reason":"工具是 5"}')
    claim = high_risk_claims("一共 3 张照片。")[0]

    review = ClaimVerifier(model).review(query="几张", claim=claim, facts=[], requirement=None)

    assert review.verdict is ClaimVerdict.CONTRADICTED
    assert review.blocks


def test_condition_review_sends_original_roles_and_object_evidence_in_one_call():
    import json

    model = _ScriptedModel('{"verdict":"ambiguous","reason":"商品价格不能证明用户购买"}')
    event = QueryCondition("event", "购买紫色裙子", "anchor")
    venue = QueryCondition("place", "Porto", "venue")
    facts = [
        Fact(
            "condition_evidence",
            RequirementValueType.DESCRIPTION,
            {"quote": "Purple skirt £59", "context": "Purple skirt £59"},
            provenance="read-2",
            source={"asset_id": "asset-1"},
        )
    ]
    review = ClaimVerifier(model).review(
        query="我买的裙子多少钱",
        claim=Claim("补证", RequirementValueType.DESCRIPTION),
        facts=facts,
        requirement=None,
        condition_updates=[(event, event), (venue, venue)],
        include_debug=True,
    )
    assert len(model.calls) == 1
    payload = json.loads(model.calls[0][1].content)
    assert payload["condition_updates"][0]["original"] == event.payload()
    assert payload["condition_updates"][1]["update"] == venue.payload()
    assert payload["facts"][0]["source"]["asset_id"] == "asset-1"
    assert review.verdict is ClaimVerdict.AMBIGUOUS
    assert review.model_call["status"] == "complete" and review.debug_call["response"]


def test_claim_verifier_fails_open_on_model_error():
    model = _ScriptedModel(RuntimeError("endpoint down"))
    claim = high_risk_claims("一共 3 张照片。")[0]

    review = ClaimVerifier(model).review(query="几张", claim=claim, facts=[], requirement=None)

    assert review.verdict is ClaimVerdict.AMBIGUOUS
    assert not review.blocks


def test_claim_call_metrics_are_recorded_without_debug():
    model = _ScriptedModel('{"verdict":"supported"}')
    review = ClaimVerifier(model).review(
        query="几张", claim=high_risk_claims("3 张照片")[0], facts=[], requirement=None
    )
    assert review.debug_call is None
    assert review.model_call["call_type"] == "claim_verifier"
    assert review.model_call["status"] == "complete"
    assert review.model_call["json_output"] == {
        "required": True,
        "attempted": True,
        "json_object_valid": True,
    }
    assert review.model_call["duration_ms"] >= 0


def test_claim_failure_preserves_metrics_and_debug_error():
    review = ClaimVerifier(_ScriptedModel(RuntimeError("endpoint down"))).review(
        query="几张", claim=high_risk_claims("3 张照片")[0], facts=[], requirement=None, include_debug=True
    )
    assert review.model_call["status"] == "error"
    assert review.model_call["json_output"] == {
        "required": True,
        "attempted": False,
        "json_object_valid": None,
    }
    assert review.model_call["error"] == "endpoint down"
    assert review.model_call["duration_ms"] >= 0
    assert review.debug_call["duration_ms"] == review.model_call["duration_ms"]
    assert review.debug_call["error"] == "endpoint down"


def test_nonstreaming_records_end_to_end_rate_without_inventing_ttft():
    from sentrix.application.assistant.debug_trace import model_timing_facts

    assert model_timing_facts({"streamed": False}, 2000, 40) == {
        "streamed": False,
        "tokens_per_second": 20.0,
        "token_rate_basis": "end_to_end",
    }
    assert model_timing_facts({}, 2000, 40) == {}
    assert model_timing_facts({"streamed": True}, 2000, 40) == {"streamed": True}
    assert "tokens_per_second" not in model_timing_facts({"streamed": False}, 0, 40)
