"""相同素材的相同证据在模型历史中只保留一次全文，原始轨迹保持完整。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from sentrix.domain.assistant.models import ChatMessage, ChatRole


def deduplicate_evidence(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """同一证据在当前上下文只保留最后一份正文，工具调用配对保持。"""
    result = list(messages)
    latest: dict[str, str] = {}
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if message.role is not ChatRole.TOOL:
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("evidence_key"):
            continue
        key = str(payload["evidence_key"])
        if payload.get("evidence_reused"):
            continue
        if key not in latest:
            latest[key] = message.tool_call_id or ""
            continue
        result[index] = replace(
            message,
            content=json.dumps(
                {
                    "evidence_reused": True,
                    "same_as_tool_call_id": latest[key],
                    "note": "相同证据正文见后续该工具结果，原完整记录保存在轨迹中。",
                },
                ensure_ascii=False,
            ),
        )
    return result
