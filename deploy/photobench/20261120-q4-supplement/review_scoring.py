"""Save the exact remaining Judge failure without changing answers or scores."""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/realmagic/xy/experiments/atm1013-20261118/q4-supplement-terminal-v1")
from inspect_terminal import BATCH, RUN, current, fingerprint

from photobench.domain.evaluation.profiles import result_key, scoring_targets, valid_result
from photobench.infrastructure.results.split_json import thaw

HERE = Path(__file__).resolve().parent
reader, state, batch = current()
revision = reader.revision
targets = scoring_targets(state, batch, "missing")
assert len(targets) == 1
qa = "atm-9b7cf030-733b-41b9-b022-605eeb625af9"
assert targets[0]["qa_id"] == qa
score = state["scoring_results"][result_key(BATCH, qa, 0)]
assert score["status"] == "failed" and score["score"] is None and not valid_result(score)
result = {
    "run_id": RUN,
    "read_revision": revision,
    "qa_id": qa,
    "activity": thaw(state["rejudge"]),
    "target_scoring_record": thaw(score),
    "all_items_reference_sha256": fingerprint(state["items"]),
    "history_count": len(score.get("history") or []),
    "score_modified": False,
    "judge_or_agent_launched": False,
}
with (HERE / "scoring-error-review.json").open("x") as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2)
print(json.dumps({"qa_id": qa, "reason": score.get("reason"), "history_count": result["history_count"]}))
