"""Read the reboot-interrupted Doubao checkpoint and existing product turns."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
HERE = Path(__file__).resolve().parent
APP = BASE / "doubao-persist-projection-v2/candidate/apps/photobench/src"
ROOT = BASE / "cloud-results"
DB = BASE / "doubao-reuse-49f347c/product-data/sentrix.db"
RUN = "20261121-132322-2026.11.17-atm-sources-addresses-current-resume-f72710"
ORIGINAL = "20261120-020624-2026.11.17-atm-sources-addresses-current-d52ecf"
PRIOR_SEED = "20261121-doubao-terminal-turns-seed-d52ecf"
BATCH = "score-dd0413c8aa869b1c84626de7"
PRODUCT = "sentrix-atm100-doubao-api.service"
EVALUATOR = "sentrix-atm100-doubao-photobench.service"
sys.path.insert(0, str(APP))

from photobench.domain.evaluation.profiles import valid_result  # noqa: E402
from photobench.infrastructure.results.split_json import STORED_TYPES  # noqa: E402
from photobench.infrastructure.results.split_store import SplitRunReader  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def api(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=90) as response:
        return json.load(response)


def pid(unit):
    return int(
        subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True)
    )


def command(process):
    return Path(f"/proc/{process}/cmdline").read_bytes().decode().strip("\0").split("\0")


def environment(process):
    return dict(
        part.split("=", 1) for part in Path(f"/proc/{process}/environ").read_text().split("\0") if "=" in part
    )


def inspect():
    assert not (HERE / "inventory.json").exists()
    reader = SplitRunReader(ROOT, ROOT / RUN)
    state = reader.state()
    assert state["status"] == "interrupted"
    assert json.loads((BASE / "active-runs.json").read_text())["doubao"]["run_id"] == RUN
    active = api(8874, "active-work")
    assert not active["work"], active
    items = {i["qa_id"]: i for i in state["items"]}
    rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    assert len(items) == 990 and len(rows) == 1013 and items.keys() <= rows.keys()
    scores = state["scoring_results"]
    valid = {k: v for k, v in scores.items() if v.get("batch_id") == BATCH and valid_result(v)}
    assert len(valid) == 823
    pids = {unit: pid(unit) for unit in (PRODUCT, EVALUATOR)}
    args = command(pids[EVALUATOR])
    assert args[args.index("--app-dir") + 1] == str(APP)
    env = environment(pids[PRODUCT])
    assert Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db" == DB
    assert env["SENTRIX_VLM_BASE_URL"] == "http://127.0.0.1:18107/v1"
    limits = {
        k: int(env[f"SENTRIX_REASONING_MAX_{k.upper()}_CALLS"])
        for k in ("model", "tool", "search", "other_tool")
    }
    assert list(limits.values()) == [100, 600, 100, 500]
    assert int(env["SENTRIX_VLM_REASONING_CONTEXT_TOKENS"]) == 16384
    runtime = state["runtime_snapshot"]
    assert state["reasoning"] and state["graph_enabled"]
    assert runtime["endpoint_model"] == "doubao-seed-2.1-lite"
    assert runtime["execution_budgets"]["qa_concurrency"] == 4
    assert runtime["execution_budgets"]["turn_timeout"] == 3660
    for key, value in limits.items():
        assert runtime["product_settings"][f"reasoning_max_{key}_calls"] == value
    with urllib.request.urlopen("http://127.0.0.1:18107/v1/models", timeout=30) as response:
        assert [m["id"] for m in json.load(response)["data"]] == ["doubao-seed-2.1-lite"]
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    prior = {}
    for old in (ORIGINAL, PRIOR_SEED):
        old_state = SplitRunReader(ROOT, ROOT / old).state()
        old_items = {i["qa_id"]: i for i in old_state["items"]}
        old_valid = {
            k: v
            for k, v in old_state["scoring_results"].items()
            if v.get("batch_id") == BATCH and valid_result(v)
        }
        prior[old] = {
            "manifest_sha256": sha(ROOT / old / "split-commit.json"),
            "item_count": len(old_items),
            "valid_scores": len(old_valid),
            "all_items_unchanged": all(fingerprint(items[q]) == fingerprint(i) for q, i in old_items.items()),
            "all_valid_scores_unchanged": all(
                fingerprint(scores[k]) == fingerprint(v) for k, v in old_valid.items()
            ),
        }
        assert prior[old]["all_items_unchanged"] and prior[old]["all_valid_scores_unchanged"], prior[old]
    assert (
        prior[ORIGINAL]["manifest_sha256"]
        == "c86fcfa0328f14a910bbb8fde2b49a5f0d22ab95bc814358db6efdbfc3d1ce07"
    )
    activity, turns = {}, []
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        activity["all_live_turns"] = [
            dict(r)
            for r in db.execute(
                "select id,scope_id,status,cancel_requested,updated_at from agent_turns "
                "where status in ('pending','running')"
            )
        ]
        activity["running_pipeline_receipts"] = [
            dict(r)
            for r in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
        activity["job_counts"] = {
            table: dict(db.execute(f"select status,count(*) from {table} group by status"))
            for table in ("import_jobs", "video_jobs")
        }
        activity["postprocess_processing"] = db.execute(
            "select count(*) from scope_postprocess where processing_generation is not null"
        ).fetchone()[0]
        for row in db.execute(
            "select t.id,t.conversation_id,c.title,t.question,t.status,t.termination_reason,"
            "t.created_at,t.updated_at,t.answer from agent_turns t "
            "join agent_conversations c on c.id=t.conversation_id "
            "where t.scope_id=? and julianday(t.created_at)>=julianday(?) order by t.created_at",
            (state["scope_id"], state["started_at"]),
        ):
            turn = dict(row)
            qa = turn.pop("title")
            if qa not in rows:
                continue
            assert turn.pop("question") == rows[qa]["question"]
            answer = turn.pop("answer") or ""
            turn.update(
                qa_id=qa, answer_chars=len(answer), answer_sha256=hashlib.sha256(answer.encode()).hexdigest()
            )
            turns.append(turn)
    activity["imports"] = api(8294, "imports/progress?scope_id=" + state["scope_id"])
    recorded = {
        t["turn_id"] for i in items.values() for t in i.get("runtime_turns") or [] if t.get("turn_id")
    }
    missing = sorted(rows.keys() - items.keys())
    attempted = {t["qa_id"] for t in turns}
    result = {
        "observed_at": datetime.now().astimezone().isoformat(),
        "run_id": RUN,
        "status": state["status"],
        "read_revision": reader.revision,
        "manifest_sha256": sha(ROOT / RUN / "split-commit.json"),
        "scope_id": state["scope_id"],
        "qa_count": len(rows),
        "item_count": len(items),
        "execution_counts": dict(Counter(i.get("execution_status") for i in items.values())),
        "termination_counts": dict(Counter(i.get("termination_reason") for i in items.values())),
        "items_fingerprint": fingerprint(state["items"]),
        "item_fingerprints": {q: fingerprint(i) for q, i in items.items()},
        "scoring_results_fingerprint": fingerprint(scores),
        "valid_scores": len(valid),
        "valid_receipt_fingerprints": {k: fingerprint(v) for k, v in valid.items()},
        "score_status_counts": dict(
            Counter(v.get("status") for v in scores.values() if v.get("batch_id") == BATCH)
        ),
        "batch_fingerprint": fingerprint(batch),
        "prior_runs": prior,
        "missing_item_qa_ids": missing,
        "product_turns": turns,
        "uncollected_product_turns": [t for t in turns if t["qa_id"] not in items],
        "unrecorded_existing_item_turns": [
            t for t in turns if t["qa_id"] in items and t["id"] not in recorded
        ],
        "never_started_qa_ids": sorted(set(missing) - attempted),
        "activity": activity,
        "service_pids": pids,
        "evaluator_app_dir": str(APP),
        "product_limits": limits,
        "reasoning_context_tokens": 16384,
        "disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "writer_source_sha256": sha(APP / "photobench/infrastructure/results/split_store.py"),
    }
    with (HERE / "inventory.json").open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "item_count",
                    "valid_scores",
                    "execution_counts",
                    "score_status_counts",
                    "service_pids",
                )
            }
            | {
                "missing": len(missing),
                "uncollected": result["uncollected_product_turns"],
                "never_started": len(result["never_started_qa_ids"]),
                "live_turns": activity["all_live_turns"],
                "prior_runs": prior,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    inspect()
