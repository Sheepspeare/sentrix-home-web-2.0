"""相同素材的相同证据在模型历史中只保留一次全文，原始轨迹保持完整。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from sentrix.domain.assistant.models import ChatMessage, ChatRole


def referenced_tool_calls(messages: Sequence[ChatMessage]) -> frozenset[str]:
    references = set()
    for message in messages:
        if message.role is not ChatRole.TOOL:
            continue
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("evidence_reused_from"), str):
            references.add(payload["evidence_reused_from"])
    return frozenset(references)


def deduplicate_evidence(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    originals: dict[str, str] = {}
    result = []
    for message in messages:
        if message.role is not ChatRole.TOOL:
            result.append(message)
            continue
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError):
            result.append(message)
            continue
        if not isinstance(payload, dict):
            result.append(message)
            continue
        key = payload.get("evidence_key")
        if isinstance(key, str) and key and key in originals:
            content = {
                field: payload[field]
                for field in ("result_set_id", "asset_handle", "reuse_hint", "evidence_key")
                if field in payload
            }
            content["evidence_reused_from"] = originals[key]
            content["note"] = "同一素材的相同证据，完整内容见前述工具结果；没有新增事实。"
            encoded = json.dumps(content, ensure_ascii=False)
            result.append(
                replace(message, content=encoded) if len(encoded) < len(message.content) else message
            )
        else:
            if (
                isinstance(key, str)
                and key
                and message.tool_call_id
                and "evidence_reused_from" not in payload
            ):
                originals[key] = message.tool_call_id
            result.append(message)
    return result
