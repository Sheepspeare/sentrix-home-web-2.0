"""Inventory the failed Doubao run and profile two isolated checkpoint writes.

No Agent/Judge request, source manifest update, service restart or resume occurs.
The isolated writer uses the existing immutable object database and its own
manifest directory, matching the deployed writer rather than changing it.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
import os
import pstats
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
APP = BASE / "wait-chat-950b2be/evaluation/apps/photobench/src"
ROOT = BASE / "cloud-results"
DB = BASE / "doubao-reuse-49f347c/product-data/sentrix.db"
RUN = "20261120-020624-2026.11.17-atm-sources-addresses-current-d52ecf"
BATCH = "score-dd0413c8aa869b1c84626de7"
HERE = Path(__file__).resolve().parent
PYTHON = Path("/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python")
sys.path.insert(0, str(APP))

from photobench.domain.evaluation.profiles import valid_result  # noqa: E402
from photobench.infrastructure.results.split_json import STORED_TYPES  # noqa: E402
from photobench.infrastructure.results.split_store import SplitResultWriter, SplitRunReader  # noqa: E402


def now():
    return datetime.now().astimezone().isoformat()


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


def save(name, value, *, exclusive=True):
    path = HERE / name
    if exclusive:
        with path.open("x") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temporary.replace(path)


def api(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=90) as response:
        return json.load(response)


def source():
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["doubao"]["run_id"] == RUN
    reader = SplitRunReader(ROOT, ROOT / RUN)
    state = reader.state()
    assert state["status"] == "failed" and state["failed_phase"] == "qa_eval"
    assert len(state["items"]) == 887 and state["qa_count"] == 1013
    assert state["reasoning"] and state["graph_enabled"]
    runtime = state["runtime_snapshot"]
    assert runtime["endpoint_model"] == "doubao-seed-2.1-lite"
    assert runtime["execution_budgets"]["qa_concurrency"] == 4
    assert runtime["execution_budgets"]["turn_timeout"] == 3660
    for key, limit in zip(("model", "tool", "search", "other_tool"), (100, 600, 100, 500), strict=True):
        assert runtime["product_settings"][f"reasoning_max_{key}_calls"] == limit
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    assert batch["qa_definition_sha256"] == "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
    return reader, state


def inventory():
    reader, state = source()
    assert not api(8874, "active-work")["work"]
    items = {i["qa_id"]: i for i in state["items"]}
    rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    assert len(items) == 887 and len(rows) == 1013 and items.keys() <= rows.keys()
    scores = state["scoring_results"]
    valid = {k: v for k, v in scores.items() if v.get("batch_id") == BATCH and valid_result(v)}
    activity = {}
    turns = []
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
            assert turn.pop("question") == rows[qa]["question"], qa
            answer = turn.pop("answer") or ""
            turn.update(
                qa_id=qa, answer_chars=len(answer), answer_sha256=hashlib.sha256(answer.encode()).hexdigest()
            )
            turns.append(turn)
    scope_live = [t for t in activity["all_live_turns"] if t["scope_id"] == state["scope_id"]]
    assert not scope_live
    activity["imports"] = api(8294, "imports/progress?scope_id=" + state["scope_id"])
    assert activity["imports"]["postprocess"]["state"] == "idle"
    assert not activity["imports"]["library"]["processing"]
    recorded_turns = {
        t["turn_id"] for i in items.values() for t in i.get("runtime_turns") or [] if t.get("turn_id")
    }
    uncollected = [t for t in turns if t["qa_id"] not in items]
    missing = sorted(rows.keys() - items.keys())
    attempted = {t["qa_id"] for t in turns}
    result = {
        "observed_at": now(),
        "run_id": RUN,
        "status": state["status"],
        "read_revision": reader.revision,
        "source_manifest_sha256": sha(ROOT / RUN / "split-commit.json"),
        "scope_id": state["scope_id"],
        "qa_count": 1013,
        "item_count": len(items),
        "execution_counts": dict(Counter(i.get("execution_status") for i in items.values())),
        "termination_counts": dict(Counter(i.get("termination_reason") for i in items.values())),
        "items_fingerprint": fingerprint(state["items"]),
        "item_fingerprints": {q: fingerprint(i) for q, i in items.items()},
        "scoring_results_fingerprint": fingerprint(scores),
        "valid_scores": len(valid),
        "valid_scores_fingerprint": fingerprint(valid),
        "valid_receipt_keys": sorted(valid),
        "score_status_counts": dict(
            Counter(v.get("status") for v in scores.values() if v.get("batch_id") == BATCH)
        ),
        "batch_fingerprint": fingerprint(next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)),
        "missing_item_qa_ids": missing,
        "uncollected_product_turns": uncollected,
        "unrecorded_turns_for_existing_items": [
            t for t in turns if t["qa_id"] in items and t["id"] not in recorded_turns
        ],
        "never_started_qa_ids": sorted(set(missing) - attempted),
        "activity": activity,
        "disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "writer_source_sha256": {
            f: sha(APP / "photobench" / f)
            for f in (
                "infrastructure/results/split_store.py",
                "infrastructure/results/split_json.py",
                "application/run_execution/state.py",
                "infrastructure/results/store.py",
            )
        },
    }
    save("inventory.json", result)
    print(
        json.dumps(
            {k: result[k] for k in ("item_count", "valid_scores", "execution_counts", "score_status_counts")}
            | {
                "missing_items": len(missing),
                "uncollected_turns": len(uncollected),
                "never_started": len(result["never_started_qa_ids"]),
                "unrecorded_existing_item_turns": len(result["unrecorded_turns_for_existing_items"]),
            }
        ),
        flush=True,
    )


def work():
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    result = {"status": "running", "worker_pid": os.getpid(), "started_at": now(), "writes": []}
    save("profile-progress.json", result)
    try:
        plan = json.loads((HERE / "inventory.json").read_text())
        reader, state = source()
        assert sha(ROOT / RUN / "split-commit.json") == plan["source_manifest_sha256"]
        assert fingerprint(state["items"]) == plan["items_fingerprint"]
        assert not api(8874, "active-work")["work"]
        target = HERE / "isolated-checkpoint" / RUN
        assert not target.exists()
        writer = SplitResultWriter(ROOT)
        for index in range(2):
            profiler = cProfile.Profile()
            began = time.monotonic()
            profiler.enable()
            try:
                receipt = writer.write(target, state)
            finally:
                profiler.disable()
                profiler.dump_stats(str(HERE / f"write-{index}.pstats"))
                output = io.StringIO()
                pstats.Stats(profiler, stream=output).strip_dirs().sort_stats("cumulative").print_stats(65)
                (HERE / f"write-{index}-profile.txt").write_text(output.getvalue())
            elapsed = time.monotonic() - began
            checked = SplitRunReader(ROOT, target)
            assert fingerprint(checked.state()["items"]) == plan["items_fingerprint"]
            assert fingerprint(checked.state()["scoring_results"]) == plan["scoring_results_fingerprint"]
            assert sha(ROOT / RUN / "split-commit.json") == plan["source_manifest_sha256"]
            result["writes"].append({"index": index, "seconds": elapsed, "receipt": receipt})
            save("profile-progress.json", result, exclusive=False)
        result.update(
            status="completed",
            accepted=True,
            source_manifest_unchanged=True,
            all_items_and_scores_unchanged=True,
            finished_at=now(),
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        save("profile-acceptance.json", result)
    except BaseException:
        result.update(status="failed", accepted=False, error=traceback.format_exc(), finished_at=now())
        save("profile-error.json", result)
        raise
    finally:
        save("profile-progress.json", result, exclusive=False)


def launch(wait_id):
    assert not (HERE / "profile-intent.json").exists()
    assert not (HERE / "profile-progress.json").exists()
    assert (HERE / "inventory.json").exists()
    assert not api(8874, "active-work")["work"]
    save(
        "profile-intent.json",
        {
            "wait_id": wait_id,
            "run_id": RUN,
            "created_at": now(),
            "script_sha256": sha(Path(__file__)),
            "agent_rerun": False,
        },
    )
    with (HERE / "profile.log").open("xb") as log:
        process = subprocess.Popen(
            [str(PYTHON), str(Path(__file__).resolve()), "work"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    result = {"worker_pid": process.pid, "wait_id": wait_id, "started_at": now(), "run_id": RUN}
    save("profile-response.json", result)
    print(json.dumps(result), flush=True)


def probe(pid):
    progress = HERE / "profile-progress.json"
    result = json.loads(progress.read_text()) if progress.exists() else {"status": "starting"}
    process = Path(f"/proc/{pid}/cmdline")
    result["worker_alive"] = (
        process.exists() and str(Path(__file__).resolve()).encode() in process.read_bytes()
    )
    result["accepted"] = (HERE / "profile-acceptance.json").exists()
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("inventory", "launch", "work", "probe"))
    parser.add_argument("--wait-id")
    parser.add_argument("--worker-pid", type=int)
    args = parser.parse_args()
    if args.mode == "inventory":
        inventory()
    elif args.mode == "launch":
        assert args.wait_id
        launch(args.wait_id)
    elif args.mode == "work":
        work()
    else:
        assert args.worker_pid
        probe(args.worker_pid)
