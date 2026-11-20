"""Complete only the sixteen saved-answer Judge gaps of the terminal Q4 run."""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from inspect_terminal import BATCH, HERE, ROOT, RUN, api, current, fingerprint, identity, save

from photobench.domain.evaluation.profiles import scoring_targets, valid_result
from photobench.infrastructure.results.split_store import SplitRunReader


def plan():
    assert not (HERE / "scoring-plan.json").exists()
    assert not api(8872, "active-work")["work"]
    inventory = json.loads((HERE / "terminal-inventory.json").read_text())
    reader, state, batch = current()
    assert reader.revision == inventory["read_revision"]
    targets = scoring_targets(state, batch, "missing")
    assert targets == inventory["missing_targets"] and len(targets) == 16
    assert not any(t.get("diagnostics_only") for t in targets)
    valid = {
        k: v for k, v in state["scoring_results"].items() if v.get("batch_id") == BATCH and valid_result(v)
    }
    assert len(valid) == inventory["valid_atm_scores"] == 963
    qa_ids = sorted(t["qa_id"] for t in targets)
    payload = {"resume_batch_ids": [BATCH], "scope": "missing", "qa_ids": qa_ids}
    preview = api(8872, f"runs/{RUN}/scoring-preview", payload)
    assert preview["qa_count"] == preview["subscore_count"] == 16
    assert preview["diagnostic_only_count"] == 0
    result = {
        "run_id": RUN,
        "source_revision": reader.revision,
        "batch_id": BATCH,
        "targets": targets,
        "payload": payload,
        "preview": preview,
        "valid_scores_before": len(valid),
        "valid_score_keys": list(valid),
        "valid_receipts_reference_sha256": fingerprint(valid),
        "all_items_reference_sha256": fingerprint(state["items"]),
        "fingerprint_scheme": "sorted_json_with_immutable_object_ids",
        "previous_target_scores": {
            t["key"]: identity(state["scoring_results"][t["key"]])
            for t in targets
            if t["key"] in state["scoring_results"]
        },
        "execution_failures": {
            i["qa_id"]: {k: i.get(k) for k in ("execution_status", "termination_reason", "error")}
            for i in state["items"]
            if i.get("execution_status") != "completed"
        },
        "agent_rerun": False,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    assert len(result["execution_failures"]) == 34
    save("scoring-plan.json", result)
    print(
        json.dumps(
            {
                "run_id": RUN,
                "valid_scores_before": len(valid),
                "judge_targets": len(targets),
                "preview": preview,
            }
        ),
        flush=True,
    )


def launch(wait_id):
    uuid.UUID(wait_id)
    assert not (HERE / "scoring-intent.json").exists()
    prepared = json.loads((HERE / "scoring-plan.json").read_text())
    assert not api(8872, "active-work")["work"]
    reader, state, batch = current()
    assert reader.revision == prepared["source_revision"]
    assert fingerprint(state["items"]) == prepared["all_items_reference_sha256"]
    assert scoring_targets(state, batch, "missing") == prepared["targets"]
    save("scoring-intent.json", {"wait_id": wait_id, **prepared})
    response = api(8872, f"runs/{RUN}/rejudge", prepared["payload"])
    save("scoring-response.json", response)
    assert response["run_id"] == RUN and response["total"] == 16 and response["status"] == "running"
    print(json.dumps(response), flush=True)


def probe(task_id):
    overview = SplitRunReader(ROOT, ROOT / RUN).overview()
    activity = overview.get("rejudge") or {}
    assert activity["rejudge_id"] == task_id
    work = api(8872, "active-work")["work"]
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {
        "run_id": RUN,
        "rejudge_id": task_id,
        **{k: activity.get(k) for k in ("status", "completed", "total", "failed", "error")},
        "worker_alive": any(w["id"] == task_id and w["worker_alive"] for w in work),
        "scoring": overview["summary"]["scoring"]["profiles"]["atm_qs_v1"],
        "disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "memory_available_kib": int(memory["MemAvailable"].split()[0]),
    }


def accept():
    assert not api(8872, "active-work")["work"]
    prepared = json.loads((HERE / "scoring-plan.json").read_text())
    response = json.loads((HERE / "scoring-response.json").read_text())
    reader, state, batch = current()
    activity = state["rejudge"]
    assert activity["rejudge_id"] == response["rejudge_id"]
    assert activity["status"] in {"completed", "completed_with_errors"}
    assert activity["completed"] == activity["total"] == 16
    previous = {k: state["scoring_results"][k] for k in prepared["valid_score_keys"]}
    assert fingerprint(previous) == prepared["valid_receipts_reference_sha256"]
    assert fingerprint(state["items"]) == prepared["all_items_reference_sha256"]
    retained_history = 0
    for key, before in prepared["previous_target_scores"].items():
        expected = {k: v for k, v in before.items() if k != "history"}
        history = state["scoring_results"][key].get("history") or []
        assert fingerprint(history[-1]) == fingerprint(expected), key
        retained_history += 1
    valid = {
        k: v for k, v in state["scoring_results"].items() if v.get("batch_id") == BATCH and valid_result(v)
    }
    remaining = scoring_targets(state, batch, "missing")
    result = {
        "run_id": RUN,
        "rejudge_id": activity["rejudge_id"],
        "read_revision": reader.revision,
        "qa_count": 1013,
        "completed_answers": 979,
        "valid_atm_scores": len(valid),
        "unchanged_previous_valid_receipts": len(previous),
        "all_answers_and_attempts_unchanged": True,
        "preserved_failed_scoring_history": retained_history,
        "retained_execution_failures": prepared["execution_failures"],
        "agent_rerun": False,
        "activity": {
            k: activity.get(k) for k in ("status", "completed", "total", "failed", "error", "results")
        },
        "remaining_targets": remaining,
        "all_judge_gaps_resolved": not remaining and len(valid) == 979,
        "scoring": reader.overview()["summary"]["scoring"]["profiles"]["atm_qs_v1"],
        "finished_at": datetime.now().astimezone().isoformat(),
    }
    save("scoring-acceptance.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "run_id",
                    "valid_atm_scores",
                    "unchanged_previous_valid_receipts",
                    "all_judge_gaps_resolved",
                    "remaining_targets",
                    "activity",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("plan", "launch", "probe", "accept"))
    parser.add_argument("--wait-id")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    if args.operation == "plan":
        plan()
    elif args.operation == "launch":
        launch(args.wait_id)
    elif args.operation == "accept":
        accept()
    else:
        print(json.dumps(probe(args.task_id)), flush=True)
