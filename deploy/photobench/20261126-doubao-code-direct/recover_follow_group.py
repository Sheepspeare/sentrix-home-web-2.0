"""Prepare one stopped Judge group from saved successes; leave the live group alone."""

import argparse
import copy
import importlib.util
import json
import shutil
import time
from pathlib import Path


def prepare(previous, output, updated, name):
    spec = importlib.util.spec_from_file_location("follow_recovery", updated)
    follow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(follow)

    def read(path):
        return json.loads(path.read_text())

    assert not output.exists(), "Inspect the existing recovery before proceeding"
    terminal = read(previous / name / "terminal.json")
    assert terminal["status"] == "failed"
    assert read(previous / "concurrency.json")["active"].get(name, 0) == 0
    plan = read(previous / "intent.json")
    group = copy.deepcopy(next(g for g in plan["groups"] if g["name"] == name))
    original = previous / group["previous_state"]
    assert follow.sha(original) == group["previous_state_sha256"]
    state = read(original)
    scores = {}
    follow.merge_scores(scores, state)
    preservation = {str(original): follow.sha(original)}
    for path in sorted((previous / name).glob("batch-*/scoring-state.json")):
        batch = read(path)
        assert batch["run_id"] == group["run_id"] and batch["qa_snapshot"] == state["qa_snapshot"]
        follow.merge_scores(scores, batch)
        for field in ("scoring_results", "diagnostic_results"):
            for key, value in batch[field].items():
                assert key not in state[field] or state[field][key] == value
                state[field][key] = value
        preservation[str(path)] = follow.sha(path)
    persisted = previous / name / "scores.json"
    assert {(s["qa_id"], s["turn_index"]): s for s in read(persisted)} == scores
    preservation[str(persisted)] = follow.sha(persisted)
    manifest = read(previous / "source-sha256.json")
    assert all(follow.sha(previous / path) == digest for path, digest in manifest.items())
    output.mkdir()
    for relative in manifest:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(previous / relative, target)
    shutil.copy2(updated, output / "follow_direct.py")
    follow.save(output / group["previous_state"], state)
    group.update(previous_state_sha256=follow.sha(output / group["previous_state"]), prior_scored=len(scores))
    plan.update(groups=[group], recovered_from=str(previous), agent_requests=0)
    follow.save(output / "intent.json", plan)
    for relative in manifest:
        manifest[relative] = follow.sha(output / relative)
    follow.save(output / "source-sha256.json", manifest)
    receipt = {
        "at": time.time(),
        "group": name,
        "preserved_successes": len(scores),
        "original_files_sha256": preservation,
        "protocol_sha256": follow.sha(output / "protocol.json"),
        "updated_follow_sha256": follow.sha(updated),
        "agent_requests": 0,
        "live_other_group_untouched": True,
    }
    assert all(follow.sha(Path(path)) == digest for path, digest in preservation.items())
    follow.save(output / "recovery-preservation.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("previous", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("updated", type=Path)
    parser.add_argument("--group", required=True, choices=["limited", "large"])
    args = parser.parse_args()
    prepare(args.previous, args.output, args.updated, args.group)
