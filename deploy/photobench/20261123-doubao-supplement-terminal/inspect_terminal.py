"""Read and accept the authorized 161-QA supplement without changing any answer."""

# ruff: noqa: E402

from __future__ import annotations

import hashlib
import json
import math
import resource
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
PRIOR = BASE / "doubao-supplement161-20261122-v1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(PRIOR))
sys.path.insert(0, str(BASE / "doubao-reconnect-20261122-v1"))
from inspect_reconnect import identity  # noqa: E402
from supplement import (  # noqa: E402
    APP as APP,
)
from supplement import (
    BATCH,
    DB,
    ROOT,
    SEED,
    SOURCE,
    fingerprint,
    idle,
    manifest_sha,
    snapshot,
    source,
)
from supplement import (
    api as api,
)

from photobench.domain.evaluation.profiles import scoring_targets, valid_result  # noqa: E402
from photobench.infrastructure.results.split_store import SplitRunReader  # noqa: E402

RUN = "20261122-215409-2026.11.17-atm-sources-addresses-current-resume-7bba84"


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def current():
    assert json.loads((BASE / "active-runs.json").read_text())["doubao"]["run_id"] == RUN
    reader = SplitRunReader(ROOT, ROOT / RUN)
    state = reader.state()
    assert state["status"] in {"completed", "completed_with_errors"}
    assert not state.get("fatal_error") and not state.get("current_phase")
    assert len(state["items"]) == state["qa_count"] == 1013
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    original, _, _, _ = source()
    frozen = next(b for b in original["scoring_batches"] if b["batch_id"] == BATCH)
    assert fingerprint(batch) == fingerprint(frozen)
    return reader, state, batch


def content(item):
    return {k: v for k, v in item.items() if k != "_index"}


def inspect():
    assert not (HERE / "terminal-inventory.json").exists()
    probe = snapshot()
    for key in (
        "exact_run_matches",
        "service_pids_match",
        "evaluation_app_matches",
        "writer_source_matches",
        "source_manifest_unchanged",
        "prior_source_manifest_unchanged",
        "prior_manifests_unchanged",
        "seed_manifest_unchanged",
        "other_stage_mapping_unchanged",
        "model_matches",
        "product_limits_match",
        "product_database_match",
    ):
        assert probe[key], key
    assert not probe["active"] and not probe["live_turns"]
    assert probe["reasoning"] and probe["graph_enabled"] and probe["reasoning_context_tokens"] == 16384
    assert probe["execution_budgets"]["qa_concurrency"] == 4
    assert probe["execution_budgets"]["turn_timeout"] == 3660
    idle_fact = idle()
    reader, state, batch = current()
    plan = json.loads((PRIOR / "plan.json").read_text())
    original, original_valid, _, targets = source()
    assert fingerprint(original["items"]) == plan["source_all_items_sha256"]
    assert fingerprint(original["scoring_results"]) == plan["source_all_scoring_receipts_sha256"]
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    items = {i["qa_id"]: i for i in state["items"]}
    old_items = {i["qa_id"]: i for i in original["items"]}
    rows = {r["qa_id"]: r for r in state["qa_snapshot"]["rows"]}
    assert items.keys() == rows.keys() and len(items) == 1013
    assert len(targets) == 161 and targets == set(plan["agent_qa_ids"])
    retained = set(items) - targets
    index_removed = []
    for qa in retained:
        assert fingerprint(old_items[qa]) == plan["retained_item_fingerprints"][qa]
        assert fingerprint(content(items[qa])) == fingerprint(content(old_items[qa])), qa
        if fingerprint(items[qa]) != fingerprint(old_items[qa]):
            assert "_index" in old_items[qa] and "_index" not in items[qa], qa
            index_removed.append(qa)
    assert len(retained) == 852
    assert fingerprint(original_valid) == plan["valid_scores_sha256"]
    for key, digest in plan["valid_receipt_fingerprints"].items():
        assert fingerprint(state["scoring_results"][key]) == digest, key
    seed = SplitRunReader(ROOT, ROOT / SEED).state()
    assert len(seed["items"]) == 852
    assert all(fingerprint(content(i)) == fingerprint(content(items[i["qa_id"]])) for i in seed["items"])
    assert all(
        fingerprint(v) == fingerprint(state["scoring_results"][k]) for k, v in seed["scoring_results"].items()
    )
    turns = []
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(
            "select t.id,t.status,t.termination_reason,t.attempt_no,t.answer,t.created_at,t.updated_at,"
            "c.title as qa_id from agent_turns t join agent_conversations c on c.id=t.conversation_id "
            "where t.scope_id=? and julianday(t.created_at)>=julianday(?) order by t.created_at",
            (state["scope_id"], state["started_at"]),
        ):
            turn = dict(row)
            answer = turn.pop("answer") or ""
            item = items[turn["qa_id"]]
            budget_row = db.execute(
                "select payload_json from agent_turn_events_v2 where turn_id=? "
                "and json_extract(payload_json,'$.kind')='request_budget' order by event_seq desc limit 1",
                (turn["id"],),
            ).fetchone()
            budget = json.loads(budget_row[0])["payload"] if budget_row else None
            turns.append(
                {
                    **turn,
                    "answer_chars": len(answer),
                    "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    "item_answer_matches": answer == (item.get("answer") or ""),
                    "recorded_turn_ids": [t.get("turn_id") for t in item.get("runtime_turns") or []],
                    "budget": budget,
                }
            )
    turn_ids = {t["id"] for t in turns}
    turn_counts = Counter(t["qa_id"] for t in turns)
    checks = {
        "exactly_161_new_product_turns": len(turns) == len(turn_ids) == 161,
        "one_new_turn_per_authorized_qa": set(turn_counts) == targets and set(turn_counts.values()) == {1},
        "one_attempt_per_new_turn": all(t["attempt_no"] == 1 for t in turns),
        "old_product_turn_ids_not_reused": not turn_ids & {t["id"] for t in plan["prior_product_turns"]},
        "all_product_turns_terminal": all(
            t["status"] in {"complete", "partial", "blocked", "failed", "cancelled"} for t in turns
        ),
        "completed_answers_match_product": all(
            t["item_answer_matches"] and t["id"] in t["recorded_turn_ids"]
            for t in turns
            if items[t["qa_id"]]["execution_status"] == "completed"
        ),
        "all_new_turn_budgets_match": all(
            t["budget"] is not None
            and [
                t["budget"][k]
                for k in ("max_model_calls", "max_tool_calls", "max_search_calls", "max_other_tool_calls")
            ]
            == [100, 600, 100, 500]
            and t["budget"]["model_attempts"] <= 100
            and t["budget"]["tool_admitted"] <= 600
            for t in turns
        ),
    }
    valid = {
        k: v for k, v in state["scoring_results"].items() if v.get("batch_id") == BATCH and valid_result(v)
    }
    missing = [t for t in scoring_targets(state, batch, "missing") if not t.get("diagnostics_only")]
    failures = {
        q: {k: items[q].get(k) for k in ("execution_status", "termination_reason", "error", "error_type")}
        for q in items
        if items[q]["execution_status"] != "completed"
    }
    new_turns = {t["qa_id"]: t for t in turns}
    comparisons = [
        {
            "qa_id": q,
            "old": {
                k: old_items[q].get(k)
                for k in ("execution_status", "termination_reason", "error", "error_type")
            },
            "new": {
                k: items[q].get(k) for k in ("execution_status", "termination_reason", "error", "error_type")
            },
            "product": new_turns.get(q),
            "valid_scores": [identity(v) for v in valid.values() if v["qa_id"] == q],
        }
        for q in sorted(targets)
    ]
    checks["execution_failures_keep_null_scores"] = not set(failures) & {v["qa_id"] for v in valid.values()}
    total_score = math.fsum(v["score"] for v in valid.values())
    result = {
        "observed_at": datetime.now().astimezone().isoformat(),
        "run_id": RUN,
        "read_revision": reader.revision,
        "manifest_sha256": manifest_sha(RUN),
        "qa_count": 1013,
        "item_count": len(items),
        "execution_counts": dict(Counter(i["execution_status"] for i in items.values())),
        "new_161_execution_counts": dict(Counter(items[q]["execution_status"] for q in targets)),
        "new_161_product_status_counts": dict(Counter(t["status"] for t in turns)),
        "new_161_product_termination_counts": dict(Counter(str(t["termination_reason"]) for t in turns)),
        "valid_atm_scores": len(valid),
        "new_valid_scores": len(valid) - 852,
        "valid_mean": total_score / len(valid),
        "coverage": len(valid) / 1013,
        "all_1013_missing_as_zero_mean": total_score / 1013,
        "valid_score_keys": list(valid),
        "valid_receipts_reference_sha256": fingerprint(valid),
        "all_items_reference_sha256": fingerprint(state["items"]),
        "all_scoring_receipts_sha256": fingerprint(state["scoring_results"]),
        "execution_failures": failures,
        "missing_targets": missing,
        "judge_failures": {
            k: identity(v)
            for k, v in state["scoring_results"].items()
            if v.get("batch_id") == BATCH and v.get("status") == "failed"
        },
        "new_attempt_comparison": comparisons,
        "checks": checks,
        "accepted": all(checks.values()),
        "original_852_answer_content_unchanged": True,
        "original_852_valid_receipts_unchanged": True,
        "source_all_items_and_all_scores_unchanged": True,
        "seed_items_and_all_scores_unchanged": True,
        "source_manifests_unchanged": True,
        "item_comparison_excluded_fields": ["_index"],
        "removed_internal_index_qa_ids": sorted(index_removed),
        "probe": probe,
        "idle": idle_fact,
        "source_run_id": SOURCE,
        "seed_run_id": SEED,
        "agent_calls_in_acceptance": 0,
    }
    save("terminal-inventory.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "run_id",
                    "read_revision",
                    "execution_counts",
                    "valid_atm_scores",
                    "new_valid_scores",
                    "new_161_product_status_counts",
                    "new_161_product_termination_counts",
                    "checks",
                    "accepted",
                    "valid_mean",
                    "coverage",
                    "all_1013_missing_as_zero_mean",
                )
            }
            | {"missing_judge_count": len(missing)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    assert result["accepted"], checks


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    inspect()
