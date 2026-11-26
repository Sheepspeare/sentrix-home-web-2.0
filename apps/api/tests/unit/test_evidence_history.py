import json

from sentrix.application.assistant.run_turn import _drop_oldest_tool_round_trip
from sentrix.domain.assistant.evidence_history import deduplicate_evidence
from sentrix.domain.assistant.models import ChatMessage, ChatRole, ToolCall


def pair(call_id, key, text, handle="photo_1"):
    return [
        ChatMessage(ChatRole.ASSISTANT, tool_calls=(ToolCall(call_id=call_id, name="read_photo_text"),)),
        ChatMessage(
            ChatRole.TOOL,
            tool_call_id=call_id,
            content=json.dumps(
                {
                    "evidence_key": key,
                    "text": text,
                    "result_set_id": "rs",
                    "asset_handle": handle,
                }
            ),
        ),
    ]


def test_identical_evidence_keeps_one_full_copy_and_all_call_ids():
    original = [
        ChatMessage(ChatRole.SYSTEM, "系统"),
        ChatMessage(ChatRole.USER, "原问题"),
        *pair("a", "same", "完整内容" * 500),
        *pair("b", "same", "完整内容" * 500, "photo_2"),
    ]
    compact = deduplicate_evidence(original)
    assert len(compact) == len(original)
    assert compact[3] == original[3]
    ref = json.loads(compact[5].content)
    assert ref["evidence_reused_from"] == "a" and ref["asset_handle"] == "photo_2"
    assert compact[5].tool_call_id == "b"
    assert len(compact[5].content) < len(original[5].content)
    assert "text" in json.loads(original[5].content)
    assert deduplicate_evidence(compact) == compact


def test_new_evidence_and_distinct_assets_are_retained():
    original = [
        *pair("a", "asset1_v1", "型号X"),
        *pair("b", "asset1_v2", "价格59"),
        *pair("c", "asset2", "型号X"),
    ]
    assert deduplicate_evidence(original) == original


def test_context_compaction_preserves_the_full_copy_referenced_by_later_messages():
    original = [
        *pair("a", "same", "重要证据" * 500),
        *pair("b", "same", "重要证据" * 500),
        *pair("c", "other", "无关往返"),
    ]
    compact = deduplicate_evidence(original)
    dropped = _drop_oldest_tool_round_trip(compact)
    assert dropped is not None
    assert any(message.tool_call_id == "a" for message in dropped)


def test_missing_evidence_key_does_not_guess_identity_from_the_handle():
    messages = [
        ChatMessage(ChatRole.TOOL, tool_call_id="a", content='{"asset_handle":"photo_1","text":"相同"}'),
        ChatMessage(ChatRole.TOOL, tool_call_id="b", content='{"asset_handle":"photo_1","text":"相同"}'),
    ]
    assert deduplicate_evidence(messages) == messages
