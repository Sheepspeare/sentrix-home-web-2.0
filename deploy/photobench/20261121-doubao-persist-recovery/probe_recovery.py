"""Read the exact resumed Doubao worker and its pinned process/answer invariants."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

from recover import APP, BASE, DB, PRODUCT, ROOT, RUN, UNIT, environment, pid, read, sha

sys.path.insert(0, str(BASE))
from probe import probe


def main():
    result = probe("doubao")
    response = read("resume-response.json")
    activation = read("activate-response.json")
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
    current = {unit: pid(unit) for unit in (PRODUCT, UNIT)}
    result["service_pids"] = current
    expected = {PRODUCT: activation["protected_pids"][PRODUCT], UNIT: activation["pid"]}
    result["service_pids_match"] = current == expected
    args = Path(f"/proc/{current[UNIT]}/cmdline").read_bytes().decode().split("\0")
    result["evaluation_app_matches"] = args[args.index("--app-dir") + 1] == str(APP)
    env = environment(current[PRODUCT])
    keys = ("MODEL", "TOOL", "SEARCH", "OTHER_TOOL")
    result["product_limits"] = {k.lower(): int(env[f"SENTRIX_REASONING_MAX_{k}_CALLS"]) for k in keys}
    result["product_limits_match"] = list(result["product_limits"].values()) == [100, 600, 100, 500]
    result["product_database_match"] = Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db" == DB
    result["reasoning_context_tokens"] = int(env["SENTRIX_VLM_REASONING_CONTEXT_TOKENS"])
    result["source_manifest_unchanged"] = (
        sha(ROOT / RUN / "split-commit.json") == read("inventory.json")["source_manifest_sha256"]
    )
    result["writer_source_matches"] = (
        sha(APP / "photobench/infrastructure/results/split_store.py")
        == read("candidate-manifest.json")["target_sha256"]
    )
    result["process_rss_kib"] = {}
    for unit, process in current.items():
        status = dict(
            line.split(":", 1)
            for line in Path(f"/proc/{process}/status").read_text().splitlines()
            if ":" in line
        )
        result["process_rss_kib"][unit] = int(status["VmRSS"].split()[0])
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    result["memory_available_kib"] = int(memory["MemAvailable"].split()[0])
    result["disk_free"] = {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")}
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        result["live_turns"] = [
            dict(r)
            for r in db.execute(
                "select id,status,updated_at,(select max(event_seq) from agent_turn_events_v2 e "
                "where e.turn_id=t.id) as event_seq "
                "from agent_turns t where scope_id=? and status in ('pending','running')",
                (result["scope_id"],),
            )
        ]
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
