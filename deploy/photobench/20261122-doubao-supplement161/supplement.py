"""One authorized new Agent attempt for each of Doubao's 161 unscored failures."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sqlite3
import sys
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "doubao-reconnect-20261122-v1"))
from inspect_reconnect import APP, DB, ROOT, fingerprint, sha  # noqa: E402
from probe_resume19 import snapshot as prior_snapshot  # noqa: E402

sys.path.insert(0, str(APP))
from photobench.domain.evaluation.profiles import summarize_batches, valid_result  # noqa: E402
from photobench.infrastructure.results.split_store import SplitResultWriter, SplitRunReader  # noqa: E402

SOURCE = "20261122-114200-2026.11.17-atm-sources-addresses-current-resume-9763a1"
REVISION = "24b94722a11249a7a67e99cbbbbb5a30"
SEED = "20261122-doubao-authorized-failed161-seed-9763a1"
BATCH = "score-dd0413c8aa869b1c84626de7"
ACCEPTED = BASE / "doubao-terminal-20261122-v1/scoring-acceptance.json"
TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}


def now():
    return datetime.now().astimezone().isoformat()


def api(port, path, payload=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def read(name):
    return json.loads((HERE / name).read_text())


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def manifest_sha(run):
    return sha(ROOT / run / "split-commit.json")


def select_targets(state):
    valid = {
        k: v for k, v in state["scoring_results"].items() if v.get("batch_id") == BATCH and valid_result(v)
    }
    failed = [i for i in state["items"] if i.get("execution_status") in {"timeout", "failed"}]
    targets = {i["qa_id"] for i in failed}
    assert not targets & {v["qa_id"] for v in valid.values()}
    return valid, failed, targets


def source():
    reader = SplitRunReader(ROOT, ROOT / SOURCE)
    assert reader.revision == REVISION
    state = reader.state()
    assert state["status"] == "completed_with_errors" and not state.get("fatal_error")
    assert state["qa_count"] == len(state["items"]) == len({i["qa_id"] for i in state["items"]}) == 1013
    assert state["reasoning"] and state["graph_enabled"]
    runtime = state["runtime_snapshot"]
    assert runtime["endpoint_model"] == "doubao-seed-2.1-lite"
    assert runtime["execution_budgets"]["qa_concurrency"] == 4
    assert runtime["execution_budgets"]["turn_timeout"] == 3660
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    assert batch["qa_definition_sha256"] == (
        "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
    )
    valid, failed, targets = select_targets(state)
    assert len(valid) == 852 and len(targets) == len(failed) == 161
    assert Counter(i["execution_status"] for i in failed) == {"timeout": 92, "failed": 69}
    accepted = json.loads(ACCEPTED.read_text())
    assert accepted["read_revision"] == REVISION and accepted["valid_atm_scores"] == 852
    assert targets == set(accepted["retained_execution_failures"])
    return state, valid, failed, targets


def idle():
    result = prior_snapshot()
    for key in (
        "service_pids_match",
        "evaluation_app_matches",
        "writer_source_matches",
        "source_manifest_unchanged",
        "prior_manifests_unchanged",
        "model_matches",
        "product_limits_match",
        "product_database_match",
    ):
        assert result[key], key
    assert not result["active"]
    assert result["reasoning_context_tokens"] == 16384
    assert result["imports"]["postprocess"]["state"] == "idle"
    assert not result["imports"]["library"]["processing"]
    assert min(result["disk_free"].values()) > 1536 * 1024**2
    assert result["memory_available_kib"] > 4 * 1024**2
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        assert (
            db.execute("select count(*) from agent_turns where status in ('pending','running')").fetchone()[0]
            == 0
        )
        for table in ("import_jobs", "video_jobs"):
            counts = dict(db.execute(f"select status,count(*) from {table} group by status"))
            assert not any(
                counts.get(s)
                for s in ("accepted", "queued", "running", "processing", "indexing", "extracting")
            ), counts
        assert (
            db.execute(
                "select count(*) from scope_postprocess where processing_generation is not null"
            ).fetchone()[0]
            == 0
        )
        pipelines = [
            dict(r)
            for r in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
        assert {r["operation_id"] for r in pipelines} <= {"8dc3d339db5e429daf3bb2865e614f39"}
        if pipelines:
            replacement = db.execute(
                "select scope_id,status,started_at from pipeline_receipts where operation_id=?",
                ("141834a96b9c4c0989ececbf326c4981",),
            ).fetchone()
            assert replacement["scope_id"] == pipelines[0]["scope_id"]
            assert (
                replacement["status"] == "completed"
                and replacement["started_at"] > pipelines[0]["started_at"]
            )
        result["all_database_turns_idle"] = True
        result["historical_pipeline_receipts"] = pipelines
    return result


def inspect():
    assert not (HERE / "plan.json").exists()
    state, valid, failed, targets = source()
    snapshot = idle()
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["doubao"]["run_id"] == SOURCE
    retained = [i for i in state["items"] if i["qa_id"] not in targets]
    assert len(retained) == 852
    turns = []
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for qa in sorted(targets):
            for row in db.execute(
                "select t.id,t.status,t.termination_reason,t.attempt_no,t.created_at,t.updated_at,t.answer "
                "from agent_turns t join agent_conversations c on c.id=t.conversation_id "
                "where t.scope_id=? and c.title=? order by t.created_at",
                (state["scope_id"], qa),
            ):
                turn = dict(row)
                answer = turn.pop("answer") or ""
                turns.append(
                    {
                        "qa_id": qa,
                        **turn,
                        "answer_chars": len(answer),
                        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    }
                )
    plan = {
        "created_at": now(),
        "authorization": (
            "User authorized rerun and re-evaluation of Doubao's 161 unfinished QA on 2026-11-22; "
            "one new attempt each. This supersedes the earlier no-Agent-retry instruction for these 161 only."
        ),
        "source_run_id": SOURCE,
        "source_read_revision": REVISION,
        "source_manifest_sha256": manifest_sha(SOURCE),
        "source_all_items_sha256": fingerprint(state["items"]),
        "source_all_scoring_receipts_sha256": fingerprint(state["scoring_results"]),
        "seed_run_id": SEED,
        "scope_id": state["scope_id"],
        "agent_qa_ids": sorted(targets),
        "retained_items_sha256": fingerprint(retained),
        "retained_item_fingerprints": {i["qa_id"]: fingerprint(i) for i in retained},
        "valid_score_keys": sorted(valid),
        "valid_scores_sha256": fingerprint(valid),
        "valid_receipt_fingerprints": {k: fingerprint(v) for k, v in valid.items()},
        "prior_attempts": [
            {
                "qa_id": i["qa_id"],
                "execution_status": i["execution_status"],
                "termination_reason": i.get("termination_reason"),
                "item_sha256": fingerprint(i),
            }
            for i in failed
        ],
        "prior_product_turns": turns,
        "resources": snapshot,
        "other_stage_mapping": {k: v for k, v in mapping.items() if k != "doubao"},
        "label": "历史豆包媒体记忆复用+新版邮件增量；161项执行失败各一次授权补做",
    }
    save("plan.json", plan)
    print(
        json.dumps(
            {
                "agent_targets": len(targets),
                "retained_answers_and_scores": 852,
                "original_failures": {"timeout": 92, "failed": 69},
                "service_pids": snapshot["service_pids"],
                "idle": True,
            }
        ),
        flush=True,
    )


def seed():
    assert not (HERE / "seed-intent.json").exists() and not (ROOT / SEED).exists()
    plan = read("plan.json")
    state, valid, _, targets = source()
    idle()
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    assert targets == set(plan["agent_qa_ids"])
    state["items"] = [i for i in state["items"] if i["qa_id"] not in targets]
    assert fingerprint(state["items"]) == plan["retained_items_sha256"]
    assert fingerprint(valid) == plan["valid_scores_sha256"]
    state["scoring_results"] = {
        k: v for k, v in state["scoring_results"].items() if v["qa_id"] not in targets
    }
    state["diagnostic_results"] = {
        k: v for k, v in (state.get("diagnostic_results") or {}).items() if v.get("qa_id") not in targets
    }
    state.pop("rejudge", None)
    state["scoring_summaries"] = summarize_batches(state)
    state.update(
        run_id=SEED,
        status="failed",
        current_phase=None,
        resumed_from=SOURCE,
        fatal_error="Authorized independent retry seed; original 161 failures remain unchanged in source.",
        authorized_doubao_supplement={
            "plan_directory": str(HERE),
            "targets": sorted(targets),
            "source_run_id": SOURCE,
        },
    )
    save(
        "seed-intent.json",
        {
            "created_at": now(),
            "source_manifest_sha256": plan["source_manifest_sha256"],
            "seed_run_id": SEED,
            "retained_answers": 852,
            "retained_valid_scores": 852,
            "new_agent_attempts": 161,
        },
    )
    SplitResultWriter(ROOT).write(ROOT / SEED, state)
    checked = SplitRunReader(ROOT, ROOT / SEED).state()
    assert len(checked["items"]) == 852 and fingerprint(checked["items"]) == plan["retained_items_sha256"]
    assert (
        fingerprint({k: checked["scoring_results"][k] for k in plan["valid_score_keys"]})
        == plan["valid_scores_sha256"]
    )
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    save(
        "seed-response.json",
        {
            "seed_run_id": SEED,
            "seed_manifest_sha256": manifest_sha(SEED),
            "inherited_answers": 852,
            "inherited_valid_scores": 852,
            "remaining_agents": 161,
            "source_manifest_unchanged": True,
        },
    )
    print(json.dumps(read("seed-response.json")), flush=True)


def resume(wait_id):
    uuid.UUID(wait_id)
    assert (HERE / "seed-response.json").exists() and not (HERE / "resume-intent.json").exists()
    plan = read("plan.json")
    state, _, _, _ = source()
    idle()
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    recovery = api(8874, f"runs/{SEED}/recovery-plan")
    assert recovery["can_resume"] and recovery["inherited_qa_count"] == 852
    assert recovery["remaining_qa_count"] == 161 and not recovery["product_version_changed"]
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["doubao"]["run_id"] == SOURCE
    assert {k: v for k, v in mapping.items() if k != "doubao"} == plan["other_stage_mapping"]
    payload = {"repair_missing_judges": False}
    save(
        "resume-intent.json",
        {
            "created_at": now(),
            "wait_id": wait_id,
            "source_run_id": SOURCE,
            "seed_run_id": SEED,
            "recovery_plan": recovery,
            "payload": payload,
        },
    )
    result = api(8874, f"runs/{SEED}/resume", payload)
    save("resume-response.json", result)
    assert result["resumed_from"] == SEED and result["resumed_items"] == 852
    assert result["scope_id"] == state["scope_id"] and not result["retried_qa_ids"]
    assert json.loads((BASE / "active-runs.json").read_text()) == mapping
    mapping["doubao"] = result
    temporary = HERE / "active-runs.next.json"
    temporary.write_text(json.dumps(mapping, indent=2))
    temporary.replace(BASE / "active-runs.json")
    print(json.dumps(result), flush=True)


def snapshot():
    response, plan = read("resume-response.json"), read("plan.json")
    result = prior_snapshot()
    result["prior_source_manifest_unchanged"] = result["source_manifest_unchanged"]
    result["source_manifest_unchanged"] = manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    result["seed_manifest_unchanged"] = (
        manifest_sha(SEED) == read("seed-response.json")["seed_manifest_sha256"]
    )
    result["exact_run_matches"] = result["run_id"] == response["run_id"]
    result["other_stage_mapping_unchanged"] = {
        k: v for k, v in json.loads((BASE / "active-runs.json").read_text()).items() if k != "doubao"
    } == plan["other_stage_mapping"]
    return result


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("inspect", "seed", "resume"))
    parser.add_argument("--wait-id")
    args = parser.parse_args()
    if args.operation == "inspect":
        inspect()
    elif args.operation == "seed":
        seed()
    else:
        resume(args.wait_id)
