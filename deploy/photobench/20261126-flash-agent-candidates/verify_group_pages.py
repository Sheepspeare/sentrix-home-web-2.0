"""Read the candidate album through production structured queries, with no model calls."""

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class ReadOnlyDatabase:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        from sentrix.domain.media.capture_time import time_sort_key
        from sentrix.infrastructure.persistence.database import _capture_in_bounds

        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.create_function(
                "capture_sort_key",
                1,
                lambda value: time_sort_key(datetime.fromisoformat(value) if value else None),
                deterministic=True,
            )
            db.create_function("capture_in_bounds", 3, _capture_in_bounds, deterministic=True)
            db.execute("pragma query_only=on")
            yield db


def verify(release, database, scope):
    sys.path.insert(0, str(release / "apps/api/src"))
    from sentrix.application.assistant.query_memory_facts import GROUP_PAGE_SIZE, QueryMemoryFacts
    from sentrix.domain.places.aliases import EMPTY_ALIASES
    from sentrix.infrastructure.persistence.repositories.structured import SqliteStructuredQueryRepository

    query = QueryMemoryFacts(
        structured=SqliteStructuredQueryRepository(ReadOnlyDatabase(database), EMPTY_ALIASES), people=None
    )
    offset, rows, pages, sizes = 0, [], 0, []
    while offset is not None:
        answer = query.execute(
            {"operation": "group", "subject": "asset", "group_by": "place", "offset": offset},
            scope_id=scope,
            viewer_id="owner",
            conversation_id="readonly-verify",
            now=datetime(2026, 11, 26),
        )
        visible = answer.visible()
        assert len(visible["value"]) <= GROUP_PAGE_SIZE and "rows" not in visible
        assert visible["total"] == answer.total and visible["group_count"] == len(answer.rows)
        rows.extend(visible["value"])
        sizes.append(len(json.dumps(visible, ensure_ascii=False)))
        pages += 1
        offset = visible["next_offset"]
    assert rows == answer.value
    return {
        "groups": len(rows),
        "total_assets": answer.total,
        "pages": pages,
        "max_model_visible_chars": max(sizes),
        "all_pages_equal_full_production_answer": True,
        "full_groups_sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest(),
        "model_requests": 0,
        "database_access": "mode=ro; query_only=on",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    result = verify(args.release, args.database, args.scope)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
