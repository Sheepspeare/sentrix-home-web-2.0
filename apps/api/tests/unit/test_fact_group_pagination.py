"""分组一次完整返回；过宽请求保留原聚合值，要求细化，不向模型分页。"""

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


def test_wide_group_output_is_not_a_partial_answer_or_a_pagination_loop():
    answer = query(Places(1660))
    visible = answer.visible()
    assert visible["status"] == "query_too_broad"
    assert visible["total"] == sum(range(1, 1661))
    assert visible["group_count"] == 1660
    assert not visible["coverage"]["complete"]
    assert not {"rows", "value", "next_offset", "offset"} & visible.keys()
    assert len(json.dumps(visible, ensure_ascii=False)) < 1500
    assert len(answer.value) == 1660


def test_narrow_group_output_is_complete_and_not_duplicated():
    answer = query(Places(4))
    visible = answer.visible()
    assert visible["status"] == "ok" and visible["coverage"]["complete"]
    assert visible["value"] == answer.value
    assert visible["total"] == 10 and visible["group_count"] == 4
    assert not {"rows", "next_offset", "offset"} & visible.keys()


def test_large_group_labels_also_require_refinement_instead_of_overflow():
    assert query(Places(64, label_chars=1000)).visible()["status"] == "query_too_broad"
