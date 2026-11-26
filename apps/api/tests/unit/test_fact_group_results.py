"""结构化分组一次完整返回；超出模型输入范围时明确拒绝部分明细。"""

import json
from datetime import datetime

from sentrix.application.assistant.query_memory_facts import QueryMemoryFacts
from sentrix.domain.retrieval.structured_query import GroupCount


class Places:
    def __init__(self, size, label_chars=2):
        self.size = size
        self.label_chars = label_chars

    def group_counts(self, _filter, group_by):
        assert group_by == "place"
        return [GroupCount(group=f"{n:04d}" + "地" * self.label_chars, count=n + 1) for n in range(self.size)]


def query(places):
    return QueryMemoryFacts(structured=places, people=None).execute(
        {"operation": "group", "subject": "asset", "group_by": "place"},
        scope_id="album",
        viewer_id="owner",
        conversation_id="c",
        now=datetime(2026, 11, 26),
    )


def test_full_small_group_set_is_returned_once_without_pagination_or_duplicate_rows():
    answer = query(Places(80))
    visible = answer.visible()
    assert visible["status"] == "ok" and visible["coverage"]["complete"]
    assert visible["total"] == sum(range(1, 81)) and visible["group_count"] == 80
    assert visible["value"] == list(answer.rows)
    assert not {"rows", "offset", "next_offset", "page_complete"} & set(visible)


def test_large_group_set_reports_overflow_without_partial_values():
    answer = query(Places(1660))
    visible = answer.visible()
    assert visible["status"] == "result_too_large"
    assert visible["value"] is None and visible["coverage"]["complete"] is False
    assert visible["group_count"] == 1660 and len(answer.rows) == 1660
    assert len(json.dumps(visible, ensure_ascii=False)) < 16000
    assert "next_offset" not in visible


def test_one_extremely_long_group_is_not_allowed_to_bypass_size_limit():
    visible = query(Places(1, label_chars=20000)).visible()
    assert visible["status"] == "result_too_large" and visible["value"] is None
