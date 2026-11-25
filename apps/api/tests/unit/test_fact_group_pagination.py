"""Large structured answers stay exact and can be read in bounded pages."""

import json
from datetime import datetime

from sentrix.application.assistant.query_memory_facts import GROUP_PAGE_SIZE, QueryMemoryFacts
from sentrix.domain.retrieval.structured_query import GroupCount


class Places:
    def group_counts(self, _filter, group_by):
        assert group_by == "place"
        return [GroupCount(group=f"{n:04d}" + "地点" * 100, count=n + 1) for n in range(1660)]


def test_all_1660_groups_are_available_without_duplicated_or_unbounded_model_output():
    query = QueryMemoryFacts(structured=Places(), people=None)
    offset = 0
    collected = []
    while offset is not None:
        answer = query.execute(
            {"operation": "group", "subject": "asset", "group_by": "place", "offset": offset},
            scope_id="album",
            viewer_id="owner",
            conversation_id="c",
            now=datetime(2026, 11, 26),
        )
        visible = answer.visible()
        assert visible["total"] == sum(range(1, 1661))
        assert visible["group_count"] == 1660
        assert visible["coverage"]["complete"]
        assert "rows" not in visible
        assert len(visible["value"]) <= GROUP_PAGE_SIZE
        assert len(json.dumps(visible, ensure_ascii=False)) < 18000
        assert len(answer.value) == 1660  # Internal evidence remains complete.
        collected.extend(visible["value"])
        offset = visible["next_offset"]
    assert collected == answer.value
