"""Read internal executor claims and error events for the 161 submitted turns."""

import json
import sqlite3
from collections import Counter

from inspect_terminal import DB, HERE, RUN, current, save

inventory = json.loads((HERE / "terminal-inventory.json").read_text())
reader, _, _ = current()
assert reader.revision == inventory["read_revision"]
details, errors = [], []
with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
    db.row_factory = sqlite3.Row
    for comparison in inventory["new_attempt_comparison"]:
        turn = comparison["product"]
        if turn["attempt_no"] > 1:
            row = dict(
                db.execute(
                    "select id,attempt_no,failure_count,execution_state,waiting_reason,fence "
                    "from agent_turns where id=?",
                    (turn["id"],),
                ).fetchone()
            )
            row["qa_id"] = comparison["qa_id"]
            row["event_attempt_counts"] = [
                dict(r)
                for r in db.execute(
                    "select attempt_no,count(*) as events,min(event_seq) as first_seq,"
                    "max(event_seq) as last_seq "
                    "from agent_turn_events_v2 where turn_id=? group by attempt_no",
                    (turn["id"],),
                )
            ]
            row["event_kinds"] = [
                dict(r)
                for r in db.execute(
                    "select attempt_no,json_extract(payload_json,'$.kind') as kind,count(*) as events "
                    "from agent_turn_events_v2 where turn_id=? group by attempt_no,kind",
                    (turn["id"],),
                )
            ]
            details.append(row)
        if turn["termination_reason"] == "model_error":
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "select payload_json from agent_turn_events_v2 where turn_id=? and "
                    "json_extract(payload_json,'$.kind') in ('error','done','termination','model_error') "
                    "order by event_seq",
                    (turn["id"],),
                )
            ]
            errors.append({"qa_id": comparison["qa_id"], "turn_id": turn["id"], "events": events})
result = {
    "run_id": RUN,
    "read_revision": reader.revision,
    "submitted_turns": 161,
    "turn_attempt_no_counts": dict(
        Counter(r["product"]["attempt_no"] for r in inventory["new_attempt_comparison"])
    ),
    "multiple_claim_turns": details,
    "model_error_events": errors,
    "additional_turns_submitted_by_this_review": 0,
}
save("attempt-review.json", result)
print(
    json.dumps(
        {
            "claim_counts": result["turn_attempt_no_counts"],
            "claims": details,
            "error_event_kinds": dict(Counter(e.get("kind") for r in errors for e in r["events"])),
        },
        ensure_ascii=False,
    ),
    flush=True,
)
