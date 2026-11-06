"""选择题「提供选项」模式的答题轮与选择解析。

开关打开后，选择题不再走盲选项加 Judge 语义匹配：答题模型在原有对话末尾收到
一轮带选项的追问，自报选项字母和一句话理由，评分端直接按字母核对答案键。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: 无原生「其他」项时补的兜底项，映射到评分侧的 ``other`` 类别。
OTHER_LABEL = "其他（以上都不是）"

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

_CHOICE_PATTERN = re.compile(r"""["']?choice["']?\s*[:：]\s*["']?([A-Za-z])["']?""")
_REASON_PATTERN = re.compile(r"""["']?reason["']?\s*[:：]\s*"((?:[^"\\]|\\.)*)\"""")


def has_native_other(options: Sequence[Mapping[str, Any]]) -> bool:
    """题目自带的「其他/以上皆非」项是否已经存在。"""
    for option in options:
        text = str(option.get("text") or "")
        if "其他" in text or "皆非" in text:
            return True
    return False


def option_labels(options: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """按顺序给选项分配字母；没有原生其他项时补一项并映射到 ``other``。"""
    labels = [
        {
            "letter": _LETTERS[index],
            "option_id": str(option.get("id")),
            "text": str(option.get("text") or "").strip(),
        }
        for index, option in enumerate(options)
    ]
    if not has_native_other(options) and len(labels) < len(_LETTERS):
        labels.append({"letter": _LETTERS[len(labels)], "option_id": "other", "text": OTHER_LABEL})
    return labels


def choice_prompt(labels: Sequence[Mapping[str, str]]) -> str:
    """追加到对话末尾的那一轮消息。题干原文不动，只追加选项与格式要求。"""
    listing = "\n".join(f"{item['letter']}. {item['text']}" for item in labels)
    return (
        "请整理前面的信息，从下列选项中选出唯一正确答案，并用 JSON 回复。\n\n"
        f"选项：\n{listing}\n\n"
        "只输出下面这个 JSON，不要附加其他文字：\n"
        '{"choice": "A", "reason": "一句话说明依据"}'
    )


def parse_choice_answer(text: str, labels: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """从最后一轮回答里取出选项。

    返回 ``{"status": "ok", "option_id": ..., "reason": ...}`` 或
    ``{"status": "failed", "reason": ...}``。解析不了就是失败，不当 0 分。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"status": "failed", "reason": "回答为空，json解析失败"}
    by_letter = {str(item["letter"]).upper(): str(item["option_id"]) for item in labels}
    choice = reason = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            choice = parsed.get("choice")
            reason = parsed.get("reason")
    except (ValueError, TypeError):
        pass
    if choice is None:
        matched = _CHOICE_PATTERN.search(raw)
        if matched:
            choice = matched.group(1)
    if reason is None:
        matched = _REASON_PATTERN.search(raw)
        if matched:
            reason = matched.group(1).replace('\\"', '"')
    letter = str(choice or "").strip().upper()
    if len(letter) > 1:
        letter = letter[:1]
    option_id = by_letter.get(letter)
    if option_id is None:
        snippet = raw[:120].replace("\n", " ")
        return {"status": "failed", "reason": f"json解析失败：未取到有效选项（原始回答：{snippet}）"}
    return {"status": "ok", "option_id": option_id, "letter": letter, "reason": str(reason or "").strip()}
