"""Distinguish copied historical conversations from this independent experiment."""

from __future__ import annotations

import json
import sqlite3

from inspect_reconnect import DB, HERE, ORIGINAL, ROOT, RUN

from photobench.infrastructure.results.split_store import SplitRunReader


def main():
    source = SplitRunReader(ROOT, ROOT / ORIGINAL).state()
    current = SplitRunReader(ROOT, ROOT / RUN).state()
    plan = json.loads((HERE / "inventory.json").read_text())
    history = {}
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for qa in plan["never_started_qa_ids"]:
            history[qa] = [
                dict(row)
                for row in db.execute(
                    "select t.id,t.conversation_id,t.status,t.created_at,t.updated_at,"
                    "length(t.answer) as answer_chars,julianday(t.created_at)>=julianday(?) as in_experiment "
                    "from agent_turns t join agent_conversations c on c.id=t.conversation_id "
                    "where t.scope_id=? and c.title=? order by t.created_at",
                    (source["started_at"], source["scope_id"], qa),
                )
            ]
    print(
        json.dumps(
            {
                "original_started_at": source["started_at"],
                "current_started_at": current["started_at"],
                "qa_history": history,
                "current_experiment_turns": sum(
                    r["in_experiment"] for rows in history.values() for r in rows
                ),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
