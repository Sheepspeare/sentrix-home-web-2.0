"""Verify the authorized 34 Q4 attempts and all retained answers and receipts."""

from __future__ import annotations

import hashlib
import json
import resource
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/realmagic/xy/experiments/atm1013-20261118/q4-supplement-v1")
import supplement as original

from photobench.domain.evaluation.profiles import (
    answer_fingerprint,
    result_key,
    scoring_targets,
    valid_result,
)
from photobench.infrastructure.results.split_store import SplitRunReader

BASE, BATCH, ROOT = original.BASE, original.BATCH, original.ROOT
api, fingerprint, identity = original.api, original.fingerprint, original.identity
HERE = Path(__file__).resolve().parent
RUN = "20261120-162112-2026.11.17-atm-sources-addresses-current-resume-93efab"
SOURCE = original.SOURCE


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def current():
    plan = original.read("plan.json")
    assert original.manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    assert json.loads((BASE / "active-runs.json").read_text())["q4km"]["run_id"] == RUN
    reader = SplitRunReader(ROOT, ROOT / RUN)
    state = reader.state()
    assert state["status"] in {"completed", "completed_with_errors"}
    assert not state.get("fatal_error") and not state.get("current_phase")
    assert len(state["items"]) == state["qa_count"] == 1013
    assert state["scope_id"] == plan["scope_id"]
    assert state["reasoning"] and state["graph_enabled"]
    runtime = state["runtime_snapshot"]
    assert runtime["endpoint_model"] == "qwen3.5-4b-q4_k_m"
    assert runtime["execution_budgets"]["qa_concurrency"] == 4
    assert runtime["execution_budgets"]["turn_timeout"] == 3660
    assert all(
        runtime["product_settings"][f"reasoning_max_{k}_calls"] == v for k, v in original.LIMITS.items()
    )
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    assert batch["qa_definition_sha256"] == (
        "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
    )
    return reader, state, batch


def main():
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    assert not (HERE / "terminal-inventory.json").exists()
    assert not api(8872, "active-work")["work"]
    plan = original.read("plan.json")
    _, source, old_valid = original.source()
    reader, state, batch = current()
    items = {i["qa_id"]: i for i in state["items"]}
    old_items = {i["qa_id"]: i for i in source["items"]}
    assert len(items) == 1013 and items.keys() == old_items.keys()
    assert items.keys() == {q["qa_id"] for q in state["qa_snapshot"]["rows"]}
    targets = set(plan["agent_qa_ids"])
    inherited = set(plan["retained_item_ids"])
    assert len(targets) == 34 and len(inherited) == 979
    assert set(state["resume_context"]["inherited_qa_ids"]) == inherited
    # Inspect references only; historical quarantined trace bodies stay unloaded.
    execution_fields = (
        "answer",
        "execution_status",
        "termination_reason",
        "attachments",
        "predicted_media",
        "scope_id",
        "tool_trace",
        "model_call_metrics",
        "trace_quarantine",
        "runtime_turns",
    )
    for qa in inherited:
        for field in execution_fields:
            assert identity(old_items[qa].get(field)) == identity(items[qa].get(field)), (qa, field)
    scores = state["scoring_results"]
    assert fingerprint(old_valid) == plan["valid_scores_sha256"]
    assert fingerprint(old_valid) == fingerprint({k: scores[k] for k in old_valid})
    markers = {q: identity(i["trace_quarantine"]) for q, i in items.items() if i.get("trace_quarantine")}
    expected_markers = {q: v for q, v in plan["source_quarantine_markers"].items() if q in inherited}
    assert markers == expected_markers and len(markers) == 14
    assert len(plan["source_quarantine_markers"]) == 18
    attempts = []
    with sqlite3.connect("file:" + str(original.DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for qa in sorted(targets):
            old, item = old_items[qa], items[qa]
            old_turns = {t["turn_id"] for t in old.get("runtime_turns") or []}
            turns = item.get("runtime_turns") or []
            new_turns = [t["turn_id"] for t in turns]
            assert new_turns and len(new_turns) == len(set(new_turns))
            assert old_turns.isdisjoint(new_turns), qa
            product_turns = []
            for turn in turns:
                row = db.execute(
                    "select id,status,termination_reason,length(answer) as answer_chars "
                    "from agent_turns where id=? and scope_id=?",
                    (turn["turn_id"], state["scope_id"]),
                ).fetchone()
                assert row is not None and row["status"] in {"completed", "failed", "cancelled"}, qa
                product_turns.append(dict(row))
            score = scores.get(result_key(BATCH, qa, 0)) or {}
            attempts.append(
                {
                    "qa_id": qa,
                    "old_execution_status": old.get("execution_status"),
                    "old_termination_reason": old.get("termination_reason"),
                    "old_turn_ids": sorted(old_turns),
                    "new_execution_status": item.get("execution_status"),
                    "new_termination_reason": item.get("termination_reason"),
                    "new_turn_ids": new_turns,
                    "product_turns": product_turns,
                    "new_answer_sha256": hashlib.sha256((item.get("answer") or "").encode()).hexdigest(),
                    "new_answer_chars": len(item.get("answer") or ""),
                    "new_score_valid": valid_result(score),
                    "new_score": score.get("score"),
                    "new_score_status": score.get("status"),
                    "historically_quarantined": qa in plan["source_quarantine_markers"],
                }
            )
    valid = {k: v for k, v in scores.items() if v.get("batch_id") == BATCH and valid_result(v)}
    failures = {}
    for qa, item in items.items():
        for idx, turn in enumerate(item.get("runtime_turns") or [item]):
            score = scores.get(result_key(BATCH, qa, idx)) or {}
            if score:
                assert score["answer_fingerprint"] == answer_fingerprint(turn), qa
        if item.get("execution_status") != "completed":
            failures[qa] = {k: item.get(k) for k in ("execution_status", "termination_reason", "error")}
    resources = original.resources()
    assert resources["model_matches"]
    for unit in original.UNITS:
        for key in ("pid", "app_dir"):
            assert resources["services"][unit][key] == plan["resources"]["services"][unit][key]
    assert resources["services"][original.UNITS[0]]["limits"] == original.LIMITS
    database = ROOT / ".split-results/objects.sqlite3"
    wal = database.with_name(database.name + "-wal")
    targets_left = scoring_targets(state, batch, "missing")
    result = {
        "run_id": RUN,
        "source_run_id": SOURCE,
        "read_revision": reader.revision,
        "status": state["status"],
        "qa_count": len(items),
        "scope_id": state["scope_id"],
        "execution_counts": dict(Counter(i.get("execution_status") for i in items.values())),
        "termination_counts": dict(Counter(i.get("termination_reason") for i in items.values())),
        "new_attempts": attempts,
        "new_attempt_count": len(attempts),
        "new_attempt_execution_counts": dict(Counter(a["new_execution_status"] for a in attempts)),
        "new_attempt_termination_counts": dict(Counter(a["new_termination_reason"] for a in attempts)),
        "new_attempt_valid_scores": sum(a["new_score_valid"] for a in attempts),
        "inherited_answers_unchanged": len(inherited),
        "inherited_valid_scores_unchanged": len(old_valid),
        "source_manifest_unchanged": True,
        "source_manifest_sha256": original.manifest_sha(SOURCE),
        "original_18_quarantines_preserved": True,
        "current_quarantine_markers": markers,
        "current_quarantined_count": len(markers),
        "valid_atm_scores": len(valid),
        "valid_score_keys": list(valid),
        "valid_receipts_sha256": fingerprint(valid),
        "all_items_sha256": fingerprint(state["items"]),
        "execution_failures": failures,
        "missing_targets": targets_left,
        "judge_only_qa_ids": sorted({t["qa_id"] for t in targets_left}),
        "source_judge_failure": identity(
            source["scoring_results"].get(result_key(BATCH, original.JUDGE_QA, 0))
        ),
        "resources": resources,
        "result_wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "product_activity": original.product_activity(state["scope_id"]),
        "scoring": reader.overview()["summary"]["scoring"]["profiles"]["atm_qs_v1"],
        "finished_at": original.now(),
        "validator_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    assert original.manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    assert SplitRunReader(ROOT, ROOT / RUN).revision == reader.revision
    save("terminal-inventory.json", result)
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                not in {
                    "new_attempts",
                    "valid_score_keys",
                    "source_judge_failure",
                    "current_quarantine_markers",
                    "product_activity",
                }
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
