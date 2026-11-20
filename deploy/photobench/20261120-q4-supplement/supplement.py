"""One authorized Q4 supplement: retain 979 answers and retry 34 failed executions.

Each mutation records a single-use intent. The accepted source stays immutable;
the saved-answer Judge gap is handled after the new Agent attempt has stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shutil
import sqlite3
import subprocess
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from photobench.domain.evaluation.profiles import summarize_batches, valid_result
from photobench.infrastructure.results.split_json import STORED_TYPES
from photobench.infrastructure.results.split_store import SplitResultWriter, SplitRunReader

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
ROOT = Path("/home/realmagic/xy/atm-bench-20261110/results")
DB = ROOT.parent / "product-data/sentrix.db"
HERE = Path(__file__).resolve().parent
SOURCE = "20261119-155444-2026.11.17-atm-sources-addresses-current-resume-3556c7"
SEED = "20261120-q4-authorized-missing34-seed-3556c7"
BATCH = "score-dd0413c8aa869b1c84626de7"
JUDGE_QA = "atm-9b7cf030-733b-41b9-b022-605eeb625af9"
LIMITS = dict(zip(("model", "tool", "search", "other_tool"), (100, 600, 100, 500), strict=True))
UNITS = ("sentrix-atm100-api.service", "sentrix-atm100-photobench.service")
TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}


def now():
    return datetime.now().astimezone().isoformat()


def api(port, path, data=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def read(name):
    return json.loads((HERE / name).read_text())


def identity(value):
    if isinstance(value, STORED_TYPES):
        return {"immutable_object_ref": value.ref}
    if isinstance(value, dict):
        return {k: identity(v) for k, v in value.items()}
    if isinstance(value, list):
        return [identity(v) for v in value]
    return value


def fingerprint(value):
    raw = json.dumps(identity(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def manifest_sha(run):
    return hashlib.sha256((ROOT / run / "split-commit.json").read_bytes()).hexdigest()


def source():
    reader = SplitRunReader(ROOT, ROOT / SOURCE)
    state = reader.state()
    assert state["status"] == "completed_with_errors"
    assert len(state["items"]) == state["qa_count"] == 1013
    assert len({i["qa_id"] for i in state["items"]}) == 1013
    assert state["reasoning"] and state["graph_enabled"]
    runtime = state["runtime_snapshot"]
    assert runtime["endpoint_model"] == "qwen3.5-4b-q4_k_m"
    assert runtime["execution_budgets"]["qa_concurrency"] == 4
    assert runtime["execution_budgets"]["turn_timeout"] == 3660
    assert all(runtime["product_settings"][f"reasoning_max_{k}_calls"] == v for k, v in LIMITS.items())
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    assert batch["qa_definition_sha256"] == (
        "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
    )
    valid = {k: v for k, v in state["scoring_results"].items() if valid_result(v)}
    assert len(valid) == 978
    return reader, state, valid


def resources():
    services = {}
    for unit in UNITS:
        pid = int(
            subprocess.check_output(
                ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True
            )
        )
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        env = dict(
            p.split("=", 1)
            for p in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0")
            if "=" in p
        )
        status = dict(
            line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line
        )
        services[unit] = {
            "pid": pid,
            "app_dir": args[args.index("--app-dir") + 1],
            "rss_kib": int(status["VmRSS"].split()[0]),
        }
        if unit == UNITS[0]:
            services[unit]["limits"] = {
                k: int(env[f"SENTRIX_REASONING_MAX_{k.upper()}_CALLS"]) for k in LIMITS
            }
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    with urllib.request.urlopen("http://127.0.0.1:8100/v1/models", timeout=30) as response:
        models = [m["id"] for m in json.load(response)["data"]]
    return {
        "services": services,
        "memory_available_kib": int(mem["MemAvailable"].split()[0]),
        "disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "models": models,
        "model_matches": models == ["qwen3.5-4b-q4_k_m"],
    }


def product_activity(scope):
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        turns = [
            dict(r)
            for r in db.execute(
                "select id,scope_id,status,cancel_requested,updated_at from agent_turns "
                "where status in ('pending','running')"
            )
        ]
        pipelines = [
            dict(r)
            for r in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
    return {
        "turns": turns,
        "pipeline_receipts": pipelines,
        "scope_turns": [t for t in turns if t["scope_id"] == scope],
        "imports": api(8291, "imports/progress?scope_id=" + scope),
    }


def idle(state):
    assert not api(8872, "active-work")["work"], "Q4 evaluator is busy"
    activity = product_activity(state["scope_id"])
    assert not activity["scope_turns"], "Q4 scope still has active turns"
    assert not [t for t in activity["turns"] if not t["cancel_requested"]]
    assert activity["imports"]["postprocess"]["state"] == "idle"
    assert not activity["imports"]["library"]["processing"]
    current = resources()
    assert current["model_matches"] and current["services"][UNITS[0]]["limits"] == LIMITS
    assert min(current["disk_free"].values()) > 1536 * 1024**2
    assert current["memory_available_kib"] > 4 * 1024**2
    return activity, current


def inspect():
    assert not (HERE / "plan.json").exists()
    reader, state, valid = source()
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["q4km"]["run_id"] == SOURCE
    activity, current = idle(state)
    failed = [i for i in state["items"] if i.get("execution_status") != "completed"]
    assert len(failed) == 34
    assert Counter(i["termination_reason"] for i in failed) == {"model_error": 27, "timeout": 7}
    targets = {i["qa_id"] for i in failed}
    assert not any(v["qa_id"] in targets for v in valid.values())
    markers = {
        i["qa_id"]: identity(i["trace_quarantine"]) for i in state["items"] if i.get("trace_quarantine")
    }
    assert len(markers) == 18 and len(targets & markers.keys()) == 4
    turns = []
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for item in failed:
            for turn in item.get("runtime_turns") or []:
                row = db.execute(
                    "select id,status,termination_reason,length(answer) as answer_chars,updated_at "
                    "from agent_turns where id=? and scope_id=?",
                    (turn["turn_id"], state["scope_id"]),
                ).fetchone()
                turns.append({"qa_id": item["qa_id"], "product_turn": dict(row) if row else None})
    candidates = [
        r
        for r in turns
        if r["product_turn"]
        and r["product_turn"]["status"] == "completed"
        and r["product_turn"]["termination_reason"] not in {"model_error", "timeout"}
        and r["product_turn"]["answer_chars"]
    ]
    retained = [i for i in state["items"] if i["qa_id"] not in targets]
    save(
        "plan.json",
        {
            "created_at": now(),
            "source_run_id": SOURCE,
            "seed_run_id": SEED,
            "source_revision": reader.revision,
            "source_manifest_sha256": manifest_sha(SOURCE),
            "source_all_items_sha256": fingerprint(state["items"]),
            "scope_id": state["scope_id"],
            "authorization": "User requested supplement of missing Q4 scores; one new attempt per failed QA, "
            "preserving accepted source answers, valid scores and failure history.",
            "agent_qa_ids": sorted(targets),
            "judge_only_qa_ids": [JUDGE_QA],
            "retained_item_ids": [i["qa_id"] for i in retained],
            "retained_items_sha256": fingerprint(retained),
            "valid_score_keys": list(valid),
            "valid_scores_sha256": fingerprint(valid),
            "source_quarantine_markers": markers,
            "quarantined_retry_qa_ids": sorted(targets & markers.keys()),
            "prior_attempts": [
                {
                    "qa_id": i["qa_id"],
                    "execution_status": i["execution_status"],
                    "termination_reason": i["termination_reason"],
                    "item_sha256": fingerprint(i),
                    "answer_sha256": hashlib.sha256((i.get("answer") or "").encode()).hexdigest(),
                }
                for i in failed
            ],
            "saved_product_turns": turns,
            "recoverable_saved_answer_candidates": candidates,
            "product_activity": activity,
            "resources": current,
            "other_stage_mapping": {k: v for k, v in mapping.items() if k != "q4km"},
            "validator_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    )
    print(
        json.dumps(
            {
                "targets": 34,
                "judge_only": 1,
                "inherited_answers": 979,
                "valid_scores_preserved": 978,
                "recoverable_candidates": candidates,
            }
        )
    )


def seed():
    assert not (HERE / "seed-intent.json").exists() and not (ROOT / SEED).exists()
    plan = read("plan.json")
    _, state, valid = source()
    idle(state)
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    assert not plan["recoverable_saved_answer_candidates"], "recover completed turns before retry"
    targets = set(plan["agent_qa_ids"])
    state["items"] = [i for i in state["items"] if i["qa_id"] not in targets]
    assert fingerprint(state["items"]) == plan["retained_items_sha256"]
    assert fingerprint(valid) == plan["valid_scores_sha256"]
    # No valid score belongs to a failed execution. Keep the existing Judge failure for later retry.
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
        fatal_error="Authorized separate supplement seed; 34 original failures remain in source.",
        authorized_q4_supplement=plan,
    )
    save(
        "seed-intent.json",
        {
            "created_at": now(),
            "source_run_id": SOURCE,
            "source_manifest_sha256": plan["source_manifest_sha256"],
            "seed_run_id": SEED,
            "retained_answers": 979,
            "retained_valid_scores": 978,
            "new_agent_attempts": 34,
            "historical_quarantines_preserved_in_source": 18,
            "inherited_current_quarantines": 14,
        },
    )
    SplitResultWriter(ROOT).write(ROOT / SEED, state)
    checked = SplitRunReader(ROOT, ROOT / SEED).state()
    assert len(checked["items"]) == 979
    assert fingerprint(checked["items"]) == plan["retained_items_sha256"]
    assert (
        fingerprint({k: checked["scoring_results"][k] for k in plan["valid_score_keys"]})
        == (plan["valid_scores_sha256"])
    )
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    save(
        "seed-response.json",
        {
            "seed_run_id": SEED,
            "remaining_agents": 34,
            "inherited_answers": 979,
            "inherited_valid_scores": 978,
            "source_manifest_unchanged": True,
        },
    )
    print(json.dumps(read("seed-response.json")))


def resume(wait_id):
    uuid.UUID(wait_id)
    assert (HERE / "seed-response.json").exists() and not (HERE / "resume-intent.json").exists()
    plan = read("plan.json")
    _, state, _ = source()
    idle(state)
    assert manifest_sha(SOURCE) == plan["source_manifest_sha256"]
    recovery = api(8872, f"runs/{SEED}/recovery-plan")
    assert recovery["can_resume"] and recovery["inherited_qa_count"] == 979
    assert recovery["remaining_qa_count"] == 34 and not recovery["product_version_changed"]
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["q4km"]["run_id"] == SOURCE
    assert {k: v for k, v in mapping.items() if k != "q4km"} == plan["other_stage_mapping"]
    # Score new answers normally; explicitly retry the single inherited Judge gap after this run.
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
    result = api(8872, f"runs/{SEED}/resume", payload)
    save("resume-response.json", result)
    assert result["resumed_from"] == SEED and result["resumed_items"] == 979
    assert result["scope_id"] == state["scope_id"] and not result["retried_qa_ids"]
    assert result["trace_quarantined_items"] == 14
    assert json.loads((BASE / "active-runs.json").read_text()) == mapping
    mapping["q4km"] = result
    temporary = HERE / "active-runs.next.json"
    temporary.write_text(json.dumps(mapping, indent=2))
    temporary.replace(BASE / "active-runs.json")
    print(json.dumps(result), flush=True)


def probe(run_id):
    response = read("resume-response.json")
    assert response["run_id"] == run_id
    overview = api(8872, f"runs/{run_id}")
    work = api(8872, "active-work")["work"]
    state = SplitRunReader(ROOT, ROOT / run_id).state()
    runtime = state.get("runtime_snapshot") or {}
    current = resources()
    current["services_match"] = all(
        current["services"][u][k] == read("plan.json")["resources"]["services"][u][k]
        for u in UNITS
        for k in ("pid", "app_dir")
    )
    current["limits_match"] = current["services"][UNITS[0]]["limits"] == LIMITS
    return {
        "stage": "q4km",
        "run_id": run_id,
        **{k: overview.get(k) for k in ("status", "current_phase", "failed_phase", "fatal_error")},
        "qa_count": state["qa_count"],
        "item_count": len(state.get("items") or []),
        "active": [{k: w.get(k) for k in ("id", "worker_alive", "status")} for w in work],
        "mapping_matches": json.loads((BASE / "active-runs.json").read_text())["q4km"]["run_id"] == run_id,
        "budget_matches": runtime.get("execution_budgets", {}).get("qa_concurrency") == 4
        and runtime.get("execution_budgets", {}).get("turn_timeout") == 3660
        and all(
            runtime.get("product_settings", {}).get(f"reasoning_max_{k}_calls") == v
            for k, v in LIMITS.items()
        ),
        "reasoning": state.get("reasoning"),
        "graph_enabled": state.get("graph_enabled"),
        "scoring": (overview.get("summary") or {}).get("selected_scoring"),
        "product_activity": product_activity(state["scope_id"]),
        "source_manifest_unchanged": manifest_sha(SOURCE) == read("plan.json")["source_manifest_sha256"],
        **current,
    }


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=["inspect", "seed", "resume", "probe"])
    parser.add_argument("--wait-id")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.operation == "probe":
        print(json.dumps(probe(args.run_id), ensure_ascii=False), flush=True)
    elif args.operation == "resume":
        resume(args.wait_id)
    else:
        {"inspect": inspect, "seed": seed}[args.operation]()
