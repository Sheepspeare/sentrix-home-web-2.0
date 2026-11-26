import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("recover_group", HERE / "recover_follow_group.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
spec = importlib.util.spec_from_file_location("follow_group", HERE / "follow_direct.py")
follow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(follow)


def test_recovery_keeps_all_success_objects_and_only_selects_stopped_group(tmp_path):
    previous, output = tmp_path / "old", tmp_path / "new"
    (previous / "large/batch-0001").mkdir(parents=True)
    write = follow.save
    base = {
        "run_id": "run",
        "qa_snapshot": {"rows": [{"qa_id": "old"}, {"qa_id": "new"}]},
        "scoring_results": {},
        "diagnostic_results": {},
    }

    def add(state, qa):
        state["scoring_results"][qa] = {
            "qa_id": qa,
            "turn_index": 0,
            "status": "completed",
            "profile_id": "atm_qs_v1",
            "is_evaluation_turn": True,
            "diagnostic_key": qa,
            "normalized_score": 1,
        }
        state["diagnostic_results"][qa] = {"status": "completed", "history": ["kept"]}

    add(base, "old")
    write(previous / "previous-large.json", base)
    batch = json.loads(json.dumps(base))
    batch["scoring_results"], batch["diagnostic_results"] = {}, {}
    add(batch, "new")
    write(previous / "large/batch-0001/scoring-state.json", batch)
    write(
        previous / "large/scores.json",
        [*base["scoring_results"].values(), *batch["scoring_results"].values()],
    )
    write(previous / "large/terminal.json", {"status": "failed"})
    write(previous / "concurrency.json", {"active": {"large": 0, "limited": 2}})
    write(previous / "protocol.json", {"enable_thinking": False})
    write(
        previous / "intent.json",
        {
            "groups": [
                {"name": "limited"},
                {
                    "name": "large",
                    "run_id": "run",
                    "previous_state": "previous-large.json",
                    "previous_state_sha256": follow.sha(previous / "previous-large.json"),
                    "prior_scored": 1,
                },
            ]
        },
    )
    manifest = {
        name: follow.sha(previous / name) for name in ["intent.json", "protocol.json", "previous-large.json"]
    }
    write(previous / "source-sha256.json", manifest)
    module.prepare(previous, output, HERE / "follow_direct.py", "large")
    plan = json.loads((output / "intent.json").read_text())
    assert len(plan["groups"]) == 1 and plan["groups"][0]["prior_scored"] == 2
    state = json.loads((output / "previous-large.json").read_text())
    assert state["scoring_results"] == {**base["scoring_results"], **batch["scoring_results"]}
    assert state["diagnostic_results"]["new"]["history"] == ["kept"]
    assert follow.sha(previous / "previous-large.json") == manifest["previous-large.json"]
    with pytest.raises(AssertionError):
        module.prepare(previous, output, HERE / "follow_direct.py", "large")
    write(previous / "concurrency.json", {"active": {"large": 1}})
    with pytest.raises(AssertionError):
        module.prepare(previous, tmp_path / "other", HERE / "follow_direct.py", "large")
