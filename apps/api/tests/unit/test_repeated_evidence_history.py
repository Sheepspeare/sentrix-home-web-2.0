import json

from sentrix.domain.assistant.evidence_history import deduplicate_evidence
from sentrix.domain.assistant.models import ChatMessage, ChatRole, ToolCall


def test_compaction_keeps_new_fields_and_tool_call_pairs():
    messages = []
    for call_id, key, text in [("r1", "same", "型号59"), ("r2", "same", "型号59"), ("r3", "new", "价格£59")]:
        messages.extend(
            [
                ChatMessage(
                    role=ChatRole.ASSISTANT,
                    content="",
                    tool_calls=(ToolCall(call_id, "read_photo_text", {}),),
                ),
                ChatMessage(
                    role=ChatRole.TOOL,
                    tool_call_id=call_id,
                    content=json.dumps({"evidence_key": key, "text": text}),
                ),
            ]
        )
    compact = deduplicate_evidence(messages)
    assert len(compact) == len(messages)
    assert [m.tool_call_id for m in compact] == [m.tool_call_id for m in messages]
    assert json.loads(compact[1].content)["same_as_tool_call_id"] == "r2"
    assert compact[3:] == messages[3:]
    assert json.loads(messages[1].content)["text"] == "型号59"
    assert deduplicate_evidence(compact) == compact
