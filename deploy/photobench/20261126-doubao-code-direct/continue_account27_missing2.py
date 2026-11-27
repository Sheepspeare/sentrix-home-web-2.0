"""Continue two confirmed unsubmitted account retries; preserve all prior versions."""

import copy
import json
import sqlite3
import time
from pathlib import Path

from representative_qa import process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")


def main():
    from representative_qa import load

    account_retry = load(
        "account_retry_current", str(B / "baseline-account27-recovery2-20261127/account_retry-current.py")
    )

    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint

    root = B / "baseline-account27-recovery2-20261127"
    target = root / "missing2-recovery1"
    assert not target.exists()
    ids = {"atm-b622ef04-2a97-4477-a779-51017c478cbc", "atm-79ab440a-5da6-4cb8-900e-368234df9c36"}
    original = B / "baseline-account27-retry-20261127"
    plan = json.loads((original / "intent.json").read_text())
    snapshot = json.loads((original / "snapshot.json").read_text())
    snapshot["rows"] = [r for r in snapshot["rows"] if r["qa_id"] in ids]
    assert len(snapshot["rows"]) == 2
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    _, _, env = process_environment(8306)
    db = sqlite3.connect((Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db").as_uri() + "?mode=ro", uri=True)
    evidence = {}
    cutoff = json.loads((B / "baseline-account27-recovery1-20261127/started.json").read_text())["at"]
    for q in ids:
        turns = db.execute(
            "select t.id,t.status,t.created_at from agent_turns t "
            "join agent_conversations c on c.id=t.conversation_id where c.title=?",
            (q,),
        ).fetchall()
        from datetime import datetime

        assert all(datetime.fromisoformat(row[2]).timestamp() < cutoff for row in turns)
        evidence[q] = turns
    target.mkdir()
    save(
        target / "selection-evidence.json",
        {
            "no_new_turns_after": cutoff,
            "old_turns": evidence,
            "reason": "POST outcome unknown after ReadTimeout; no retry turn in product DB",
            "at": time.time(),
        },
    )
    plan = copy.deepcopy(plan)
    plan["group"]["missing_ids"] = sorted(ids)
    plan["group"]["qa_count"] = 2
    save(target / "intent.json", plan)
    save(target / "snapshot.json", snapshot)
    account_retry.launch(target)


if __name__ == "__main__":
    main()
