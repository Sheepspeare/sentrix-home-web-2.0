"""完整聚合值按页返回，保留统计总量和后续页位置。"""

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


def test_all_group_pages_recover_complete_aggregation_without_duplicating_rows():
    from dataclasses import replace

    answer = query(Places(1660))
    rows = []
    offset = 0
    while True:
        visible = replace(answer, offset=offset).visible()
        assert visible["status"] == "ok" and visible["coverage"]["complete"]
        assert visible["total"] == sum(range(1, 1661)) and visible["group_count"] == 1660
        assert "rows" not in visible and len(visible["value"]) <= 64
        rows.extend(visible["value"])
        if visible["next_offset"] is None:
            break
        assert visible["next_offset"] > offset
        offset = visible["next_offset"]
    assert rows == list(answer.rows)


def test_long_group_labels_reduce_page_size_without_changing_the_query():
    visible = query(Places(64, label_chars=1000)).visible()
    assert 0 < len(visible["value"]) < 64
    assert len(json.dumps(visible["value"], ensure_ascii=False)) < 16000
    assert visible["next_offset"] == len(visible["value"])
    assert visible["group_count"] == 64
