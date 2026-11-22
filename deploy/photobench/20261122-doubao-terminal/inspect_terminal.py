"""Accept the exact Doubao terminal checkpoint without expanding stored traces."""

from __future__ import annotations

import hashlib
import json
import resource
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
PRIOR = BASE / "doubao-reconnect-20261122-v1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(PRIOR))
from inspect_reconnect import APP, BATCH, DB, ROOT, api, fingerprint, identity, sha  # noqa: E402
from probe_resume19 import snapshot  # noqa: E402

sys.path.insert(0, str(APP))
from photobench.domain.evaluation.profiles import scoring_targets, valid_result  # noqa: E402
from photobench.infrastructure.results.split_store import SplitRunReader  # noqa: E402

RUN = "20261122-114200-2026.11.17-atm-sources-addresses-current-resume-9763a1"


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def current():
    assert json.loads((BASE / "active-runs.json").read_text())["doubao"]["run_id"] == RUN
    reader = SplitRunReader(ROOT, ROOT / RUN)
    state = reader.state()
    assert state["status"] == "completed_with_errors" and not state.get("fatal_error")
    assert len(state["items"]) == state["qa_count"] == 1013
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    assert batch["qa_definition_sha256"] == (
        "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
    )
    return reader, state, batch


def inspect():
    assert not (HERE / "terminal-inventory.json").exists()
    probe = snapshot()
    for key in (
        "exact_run_matches",
        "service_pids_match",
        "evaluation_app_matches",
        "source_manifest_unchanged",
        "prior_manifests_unchanged",
        "writer_source_matches",
        "model_matches",
        "product_limits_match",
        "product_database_match",
    ):
        assert probe[key], key
    assert not probe["active"] and not probe["live_turns"]
    assert not api(8874, "active-work")["work"]
    assert probe["execution_budgets"]["qa_concurrency"] == 4
    assert probe["execution_budgets"]["turn_timeout"] == 3660
    assert probe["reasoning"] and probe["graph_enabled"]
    assert probe["reasoning_context_tokens"] == 16384
    reader, state, batch = current()
    old = json.loads((PRIOR / "inventory.json").read_text())
    restored = json.loads((PRIOR / "restore-four-response.json").read_text())
    items = {i["qa_id"]: i for i in state["items"]}
    rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    assert len(items) == 1013 and items.keys() == rows.keys()
    original = SplitRunReader(ROOT, ROOT / old["run_id"]).state()
    original_items = {i["qa_id"]: i for i in original["items"]}
    differences = json.loads((HERE / "preservation-differences.json").read_text())
    assert all(set(fact["field_counts"]) == {"_index"} for fact in differences["sources"].values())

    def content(item):
        return {k: v for k, v in item.items() if k != "_index"}

    for qa, digest in old["item_fingerprints"].items():
        assert fingerprint(original_items[qa]) == digest, qa
        assert fingerprint(content(items[qa])) == fingerprint(content(original_items[qa])), qa
    for key, digest in old["valid_receipt_fingerprints"].items():
        assert fingerprint(state["scoring_results"][key]) == digest, key
    seed_reader = SplitRunReader(ROOT, ROOT / restored["seed_run_id"])
    seed = seed_reader.state()
    assert len(seed["items"]) == 994
    assert all(fingerprint(content(items[i["qa_id"]])) == fingerprint(content(i)) for i in seed["items"])
    assert all(
        fingerprint(state["scoring_results"][k]) == fingerprint(v) for k, v in seed["scoring_results"].items()
    )
    for fact in restored["restored"]:
        item = items[fact["qa_id"]]
        assert hashlib.sha256(item["answer"].encode()).hexdigest() == fact["answer_sha256"]
        assert fact["id"] in [t["turn_id"] for t in item["runtime_turns"]]
    target_ids = set(old["never_started_qa_ids"])
    assert len(target_ids) == 19
    assert set(items) - {i["qa_id"] for i in seed["items"]} == target_ids
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        live = [
            dict(r)
            for r in db.execute(
                "select id,scope_id,status from agent_turns where status in ('pending','running')"
            )
        ]
        assert not live
        turns = [
            dict(r)
            for r in db.execute(
                "select t.id,t.status,t.termination_reason,t.attempt_no,t.answer,c.title as qa_id "
                "from agent_turns t join agent_conversations c on c.id=t.conversation_id "
                "where t.scope_id=? and julianday(t.created_at)>=julianday(?)",
                (state["scope_id"], state["started_at"]),
            )
        ]
        assert len(turns) == 19 and {t["qa_id"] for t in turns} == target_ids
        for turn in turns:
            item = items[turn["qa_id"]]
            assert turn["id"] in [t["turn_id"] for t in item["runtime_turns"]]
            assert turn["attempt_no"] == 1
            assert turn["status"] in {"complete", "partial", "failed", "cancelled"}
            assert turn["answer"] == item["answer"]
            turn["answer_sha256"] = hashlib.sha256(turn.pop("answer").encode()).hexdigest()
            turn["execution_status"] = item["execution_status"]
            events = db.execute(
                "select payload_json from agent_turn_events_v2 where turn_id=? "
                "and json_extract(payload_json,'$.kind')='request_budget' order by event_seq desc limit 1",
                (turn["id"],),
            ).fetchall()
            assert events, turn["id"]
            budget = json.loads(events[0][0])["payload"]
            assert [
                budget[k]
                for k in ("max_model_calls", "max_tool_calls", "max_search_calls", "max_other_tool_calls")
            ] == [100, 600, 100, 500]
            assert budget["model_attempts"] <= 100 and budget["tool_admitted"] <= 600
            turn["budget"] = budget
        pipelines = [
            dict(r)
            for r in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
        jobs = {
            table: dict(db.execute(f"select status,count(*) from {table} group by status"))
            for table in ("import_jobs", "video_jobs")
        }
        processing = db.execute(
            "select count(*) from scope_postprocess where processing_generation is not null"
        ).fetchone()[0]
    valid = {
        k: v for k, v in state["scoring_results"].items() if v.get("batch_id") == BATCH and valid_result(v)
    }
    missing = [t for t in scoring_targets(state, batch, "missing") if not t.get("diagnostics_only")]
    failures = {
        i["qa_id"]: {k: i.get(k) for k in ("execution_status", "termination_reason", "error")}
        for i in state["items"]
        if i["execution_status"] != "completed"
    }
    result = {
        "observed_at": datetime.now().astimezone().isoformat(),
        "run_id": RUN,
        "read_revision": reader.revision,
        "manifest_sha256": sha(ROOT / RUN / "split-commit.json"),
        "qa_count": 1013,
        "item_count": len(items),
        "execution_counts": dict(Counter(i["execution_status"] for i in items.values())),
        "termination_counts": dict(Counter(str(i.get("termination_reason")) for i in items.values())),
        "valid_atm_scores": len(valid),
        "valid_score_keys": list(valid),
        "valid_receipts_reference_sha256": fingerprint(valid),
        "all_items_reference_sha256": fingerprint(state["items"]),
        "execution_failures": failures,
        "missing_targets": missing,
        "missing_answers": [
            {
                "qa_id": t["qa_id"],
                "answer_chars": len(items[t["qa_id"]]["answer"]),
                "execution_status": items[t["qa_id"]]["execution_status"],
                "old_score": identity(state["scoring_results"].get(t["key"])),
            }
            for t in missing
        ],
        "judge_failures": {
            k: identity(v)
            for k, v in state["scoring_results"].items()
            if v.get("batch_id") == BATCH and v.get("status") == "failed"
        },
        "new_nineteen_turns": turns,
        "restored_four": restored["restored"],
        "original_990_items_unchanged": True,
        "item_comparison_excluded_fields": ["_index"],
        "removed_index_metadata_items": 987,
        "original_823_valid_scores_unchanged": True,
        "seed_994_items_and_all_score_receipts_unchanged": True,
        "source_manifests_unchanged": True,
        "seed_manifest_sha256": sha(ROOT / restored["seed_run_id"] / "split-commit.json"),
        "all_product_live_turns": live,
        "running_pipeline_receipts": pipelines,
        "job_counts": jobs,
        "postprocess_processing": processing,
        "probe": probe,
        "agent_rerun": False,
    }
    save("terminal-inventory.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "run_id",
                    "read_revision",
                    "item_count",
                    "execution_counts",
                    "valid_atm_scores",
                    "original_990_items_unchanged",
                    "original_823_valid_scores_unchanged",
                )
            }
            | {"judge_targets": len(missing), "target_ids": [t["qa_id"] for t in missing]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    inspect()
