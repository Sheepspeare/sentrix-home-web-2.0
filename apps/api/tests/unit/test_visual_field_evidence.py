from __future__ import annotations

import json
from dataclasses import replace

import pytest

from sentrix.domain.assistant.claim_verifier import (
    ClaimVerdict,
    build_claim_messages,
    build_field_messages,
    field_review_facts,
    high_risk_claims,
    parse_field_reviews,
)
from sentrix.domain.assistant.evidence_binding import BindingVerdict, Fact, bind_requirement, facts_from_state
from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement, EvidenceType, RequirementValueType
from sentrix.domain.assistant.profile import DEFAULT_PROFILE
from sentrix.domain.assistant.task_state import TaskState, ToolCallRecord

REQ = EvidenceRequirement(
    "seat", EvidenceType.VISIBLE_TEXT, "读取座位号", predicate="座位号", value_type=RequirementValueType.VALUE
)
OCR = Fact(
    "visible_text",
    RequirementValueType.DESCRIPTION,
    "座位 12A；票价 £59",
    provenance="ocr",
    source={
        "asset_id": "ticket",
        "result_set_id": "old",
        "asset_handle": "photo_2",
        "origin": "ingestion_ocr",
    },
)


def response(*rows):
    return json.dumps({"verdict": "supported", "fields": list(rows)}, ensure_ascii=False)


def supported(req="seat", call="ocr", quote="座位 12A"):
    return {
        "requirement_id": req,
        "verdict": "supported",
        "value": "12A",
        "citations": [{"call_id": call, "quote": quote}],
    }


def test_tool_success_does_not_bind_an_arbitrary_observation_to_a_field():
    assert bind_requirement(REQ, [OCR]).verdict is BindingVerdict.UNKNOWN
    description = replace(
        REQ, evidence_type=EvidenceType.VISUAL_OBSERVATION, value_type=RequirementValueType.DESCRIPTION
    )
    assert (
        bind_requirement(
            description, [replace(OCR, fact_type="visual_observation", value="白色车票")]
        ).verdict
        is BindingVerdict.UNKNOWN
    )


def test_ocr_amount_and_visual_observation_reach_existing_claim_check():
    messages = build_claim_messages(
        query="票价多少", claim=high_risk_claims("票价59元")[0], facts=[OCR], requirement=REQ
    )
    assert "票价 £59" in messages[1]["content"]
    assert "ingestion_ocr" in messages[1]["content"]


def test_field_check_carries_question_predicate_and_original_image_reference():
    messages = build_field_messages(query="票据的座位号和金额", answer="12A", facts=[OCR], requirements=[REQ])
    payload = json.loads(messages[1]["content"])
    assert payload["requirements"][0]["predicate"] == "座位号"
    assert payload["facts"][0]["source"] == OCR.source
    assert payload["facts"][0]["value"] == OCR.value


def test_readable_ocr_can_satisfy_field_without_another_visual_call():
    result = parse_field_reviews(response(supported()), requirements=[REQ], facts=[OCR])[0]
    assert result.verdict is ClaimVerdict.SUPPORTED
    assert result.citations == (("ocr", "座位 12A"),)


def test_ocr_line_wrapping_preserves_the_original_quote():
    fact = replace(OCR, value="Model ABC\nSkirt £59")
    result = parse_field_reviews(
        response(supported(quote="Model ABC Skirt")), requirements=[REQ], facts=[fact]
    )[0]
    assert result.verdict is ClaimVerdict.SUPPORTED
    assert result.citations == (("ocr", "Model ABC\nSkirt"),)


def test_rewritten_citation_requests_citation_repair_instead_of_new_image():
    result = parse_field_reviews(response(supported(quote="座位...12A")), requirements=[REQ], facts=[OCR])[0]
    assert result.verdict is ClaimVerdict.AMBIGUOUS
    assert result.citation_error
    assert not result.citations
    uncertain = parse_field_reviews(
        response(supported(quote="座位...12A")), requirements=[REQ], facts=[replace(OCR, confidence=0.6)]
    )[0]
    assert not uncertain.citation_error


@pytest.mark.parametrize(
    "fact",
    [
        replace(OCR, confidence=0.6),
        replace(OCR, filters_not_applied=("person",)),
        replace(OCR, value="白色车票"),
        replace(OCR, provenance="other"),
    ],
)
def test_uncertain_unapplied_or_wrong_source_cannot_confirm_field(fact):
    result = parse_field_reviews(response(supported()), requirements=[REQ], facts=[fact])[0]
    assert result.verdict is ClaimVerdict.AMBIGUOUS


def test_two_fields_of_same_type_are_reviewed_separately():
    req2 = replace(REQ, id="carriage", predicate="车厢号")
    raw = response(
        supported(), {"requirement_id": "carriage", "verdict": "unsupported", "reason": "车厢号未读到"}
    )
    results = parse_field_reviews(raw, requirements=[REQ, req2], facts=[OCR])
    assert [item.verdict for item in results] == [ClaimVerdict.SUPPORTED, ClaimVerdict.UNSUPPORTED]


def test_missing_duplicate_and_invented_citations_stay_unconfirmed():
    for raw in (
        response(),
        response(supported(), supported()),
        response(supported(call="fabricated")),
        "not json",
    ):
        assert parse_field_reviews(raw, requirements=[REQ], facts=[OCR])[0].verdict is ClaimVerdict.AMBIGUOUS
    row = supported()
    row["citations"].append({"call_id": "invented", "quote": "车厢2"})
    assert (
        parse_field_reviews(response(row), requirements=[REQ], facts=[OCR])[0].verdict
        is ClaimVerdict.AMBIGUOUS
    )


def test_repeated_evidence_deduplicates_but_new_field_and_new_image_remain():
    facts = [
        OCR,
        replace(OCR, provenance="ocr2"),
        replace(OCR, value="车厢 2"),
        replace(OCR, source={"asset_id": "another"}),
    ]
    selected = field_review_facts(facts)
    assert len(selected) == 3
    assert {fact.value for fact in selected} == {OCR.value, "车厢 2"}


def test_field_parser_handles_braces_inside_quotes():
    fact = replace(OCR, value="型号 {AB}")
    assert (
        parse_field_reviews(response(supported(quote="型号 {AB}")), requirements=[REQ], facts=[fact])[
            0
        ].verdict
        is ClaimVerdict.SUPPORTED
    )


def test_reading_a_pending_candidate_preserves_unapplied_conditions():
    state = TaskState(turn_id="t", conversation_id="c", scope_id="s", question="q", profile=DEFAULT_PROFILE)
    state.record(
        ToolCallRecord(
            call_id="search",
            tool="search_memories",
            signature="s",
            status="ok",
            observation={"asset_ids": ["ticket"], "returned_asset_ids": ["ticket"]},
            model_visible={"answer_eligible": False},
        )
    )
    state.record(
        ToolCallRecord(
            call_id="ocr",
            tool="read_photo_text",
            signature="r",
            status="ok",
            observation={"asset_id": "ticket", "result_set_id": "old", "asset_handle": "photo_2"},
            model_visible={"full_text": OCR.value},
        )
    )
    fact = facts_from_state(state)[0]
    assert fact.source["asset_id"] == "ticket"
    assert fact.source["result_set_id"] == "old"
    assert fact.filters_not_applied == ("query_conditions",)
    assert (
        parse_field_reviews(response(supported()), requirements=[REQ], facts=[fact])[0].verdict
        is ClaimVerdict.AMBIGUOUS
    )
