"""Observe four original product turns reclaimed by the durable executor."""

from __future__ import annotations

import json
import shutil
import sqlite3
import urllib.request
from pathlib import Path

from inspect_reconnect import (
    APP,
    BASE,
    DB,
    EVALUATOR,
    HERE,
    ORIGINAL,
    PRODUCT,
    ROOT,
    RUN,
    api,
    command,
    environment,
    pid,
    sha,
)


def snapshot():
    plan = json.loads((HERE / "inventory.json").read_text())
    facts = plan["uncollected_product_turns"]
    assert len(facts) == 4
    processes = {unit: pid(unit) for unit in (PRODUCT, EVALUATOR)}
    args = command(processes[EVALUATOR])
    env = environment(processes[PRODUCT])
    limits = {
        k: int(env[f"SENTRIX_REASONING_MAX_{k.upper()}_CALLS"])
        for k in ("model", "tool", "search", "other_tool")
    }
    with urllib.request.urlopen("http://127.0.0.1:18107/v1/models", timeout=20) as response:
        models = [m["id"] for m in json.load(response)["data"]]
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        turns = []
        for fact in facts:
            row = db.execute(
                "select id,conversation_id,status,termination_reason,created_at,updated_at,"
                "attempt_no,execution_state,waiting_reason,length(answer) as answer_chars,"
                "(select max(event_seq) from agent_turn_events_v2 e where e.turn_id=t.id) as event_seq "
                "from agent_turns t where id=?",
                (fact["id"],),
            ).fetchone()
            assert row and row["conversation_id"] == fact["conversation_id"]
            turns.append(dict(row) | {"qa_id": fact["qa_id"]})
        live = [
            dict(row)
            for row in db.execute(
                "select id,scope_id,status,updated_at from agent_turns where status in ('pending','running')"
            )
        ]
        pipelines = [
            dict(row)
            for row in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    rss = {}
    for unit, process in processes.items():
        fields = dict(
            line.split(":", 1)
            for line in Path(f"/proc/{process}/status").read_text().splitlines()
            if ":" in line
        )
        rss[unit] = int(fields["VmRSS"].split()[0])
    manifest = json.loads((ROOT / RUN / "split-commit.json").read_text())
    return {
        "run_id": RUN,
        "stage": "doubao_existing_four_turns",
        "turns": turns,
        "all_terminal": all(t["status"] not in ("pending", "running") for t in turns),
        "all_live_turns": live,
        "running_pipeline_receipts": pipelines,
        "active_evaluation": api(8874, "active-work")["work"],
        "imports": api(8294, "imports/progress?scope_id=" + plan["scope_id"]),
        "service_pids": processes,
        "service_pids_match": processes == plan["service_pids"],
        "evaluation_app_matches": args[args.index("--app-dir") + 1] == str(APP),
        "writer_source_matches": sha(APP / "photobench/infrastructure/results/split_store.py")
        == plan["writer_source_sha256"],
        "source_manifest_unchanged": sha(ROOT / RUN / "split-commit.json") == plan["manifest_sha256"],
        "original_manifest_unchanged": sha(ROOT / ORIGINAL / "split-commit.json")
        == plan["prior_runs"][ORIGINAL]["manifest_sha256"],
        "active_mapping_matches": json.loads((BASE / "active-runs.json").read_text())["doubao"]["run_id"]
        == RUN,
        "model_matches": models == ["doubao-seed-2.1-lite"],
        "product_limits": limits,
        "product_limits_match": list(limits.values()) == [100, 600, 100, 500],
        "reasoning_context_tokens": int(env["SENTRIX_VLM_REASONING_CONTEXT_TOKENS"]),
        "product_database_match": Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db" == DB,
        "process_rss_kib": rss,
        "memory_available_kib": int(memory["MemAvailable"].split()[0]),
        "disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "checkpoint_status": manifest["monitor"]["status"],
        "item_count": plan["item_count"],
        "valid_scores": plan["valid_scores"],
        "never_started_count": len(plan["never_started_qa_ids"]),
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False), flush=True)
