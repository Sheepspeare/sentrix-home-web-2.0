"""Read the exact nineteen-QA continuation and retained source checkpoints."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

from inspect_reconnect import (
    APP,
    BASE,
    DB,
    EVALUATOR,
    HERE,
    PRODUCT,
    ROOT,
    RUN,
    command,
    environment,
    pid,
    sha,
)

sys.path.insert(0, str(BASE))
from probe import probe  # noqa: E402


def snapshot():
    result = probe("doubao")
    plan = json.loads((HERE / "inventory.json").read_text())
    response = json.loads((HERE / "resume19-response.json").read_text())
    selected = (result.pop("summary", {}) or {}).get("selected_scoring", {})
    result["scoring"] = {
        k: selected.get(k)
        for k in (
            "batch_id",
            "rule_version",
            "qa_definition_sha256",
            "total",
            "scored",
            "failed",
            "status",
            "scored_mean",
        )
    }
    result["exact_run_matches"] = result["run_id"] == response["run_id"]
    current = {unit: pid(unit) for unit in (PRODUCT, EVALUATOR)}
    result["service_pids"] = current
    result["service_pids_match"] = current == plan["service_pids"]
    args = command(current[EVALUATOR])
    result["evaluation_app_matches"] = args[args.index("--app-dir") + 1] == str(APP)
    env = environment(current[PRODUCT])
    result["product_limits"] = {
        k: int(env[f"SENTRIX_REASONING_MAX_{k.upper()}_CALLS"])
        for k in ("model", "tool", "search", "other_tool")
    }
    result["product_limits_match"] = list(result["product_limits"].values()) == [100, 600, 100, 500]
    result["product_database_match"] = Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db" == DB
    result["reasoning_context_tokens"] = int(env["SENTRIX_VLM_REASONING_CONTEXT_TOKENS"])
    result["source_manifest_unchanged"] = sha(ROOT / RUN / "split-commit.json") == plan["manifest_sha256"]
    result["prior_manifests_unchanged"] = all(
        sha(ROOT / old / "split-commit.json") == fact["manifest_sha256"]
        for old, fact in plan["prior_runs"].items()
    )
    result["writer_source_matches"] = (
        sha(APP / "photobench/infrastructure/results/split_store.py") == plan["writer_source_sha256"]
    )
    result["process_rss_kib"] = {}
    for unit, process in current.items():
        fields = dict(
            line.split(":", 1)
            for line in Path(f"/proc/{process}/status").read_text().splitlines()
            if ":" in line
        )
        result["process_rss_kib"][unit] = int(fields["VmRSS"].split()[0])
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    result["memory_available_kib"] = int(memory["MemAvailable"].split()[0])
    result["disk_free"] = {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")}
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        result["live_turns"] = [
            dict(row)
            for row in db.execute(
                "select id,status,updated_at,(select max(event_seq) from agent_turn_events_v2 e "
                "where e.turn_id=t.id) as event_seq "
                "from agent_turns t where scope_id=? and status in ('pending','running')",
                (result["scope_id"],),
            )
        ]
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False), flush=True)
