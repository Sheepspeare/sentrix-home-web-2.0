"""Inspect the exact terminal Q4 run without expanding stored trace bodies."""

from __future__ import annotations

import hashlib
import json
import resource
import shutil
import sqlite3
import subprocess
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from photobench.domain.evaluation.profiles import (
    answer_fingerprint,
    result_key,
    scoring_targets,
    valid_result,
)
from photobench.infrastructure.results.split_json import STORED_TYPES
from photobench.infrastructure.results.split_store import SplitRunReader

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
ROOT = Path("/home/realmagic/xy/atm-bench-20261110/results")
RUN = "20261119-155444-2026.11.17-atm-sources-addresses-current-resume-3556c7"
PARENT = "20261119-154238-2026.11.17-atm-sources-addresses-current-resume-b5c51a"
BATCH = "score-dd0413c8aa869b1c84626de7"
APP = BASE / "psh-formal-f75c105/evaluation/apps/photobench/src"
HERE = Path(__file__).resolve().parent


def api(port, path, payload=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def identity(value):
    if isinstance(value, STORED_TYPES):
        return {"immutable_object_ref": value.ref}
    if isinstance(value, dict):
        return {k: identity(v) for k, v in value.items()}
    if isinstance(value, list):
        return [identity(v) for v in value]
    return value


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def pid(unit):
    return int(
        subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True)
    )


def resources():
    result = {"disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")}}
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    result["memory_available_kib"] = int(memory["MemAvailable"].split()[0])
    result["services"] = {}
    for unit in ("sentrix-atm100-api.service", "sentrix-atm100-photobench.service"):
        process = pid(unit)
        args = Path(f"/proc/{process}/cmdline").read_bytes().decode().split("\0")
        fields = dict(
            line.split(":", 1)
            for line in Path(f"/proc/{process}/status").read_text().splitlines()
            if ":" in line
        )
        env = dict(
            p.split("=", 1)
            for p in Path(f"/proc/{process}/environ").read_bytes().decode().split("\0")
            if "=" in p
        )
        result["services"][unit] = {
            "pid": process,
            "app_dir": args[args.index("--app-dir") + 1],
            "memory": {k: fields.get(k, "").strip() for k in ("VmRSS", "VmHWM")},
            "limits": {
                k: env.get("SENTRIX_REASONING_MAX_" + k.upper() + "_CALLS")
                for k in ("model", "tool", "search", "other_tool")
            },
        }
    result["gpu"] = subprocess.check_output(
        ["/usr/bin/nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_gpu_memory", "--format=csv,noheader"],
        text=True,
    ).splitlines()
    return result


def current():
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["q4km"]["run_id"] == RUN
    reader = SplitRunReader(ROOT, ROOT / RUN)
    state = reader.state()
    assert state["status"] == "completed_with_errors" and not state.get("fatal_error")
    assert len(state["items"]) == state["qa_count"] == 1013
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    assert batch["qa_definition_sha256"] == "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
    assert state["reasoning"] and state["graph_enabled"]
    budgets = state["runtime_snapshot"]["execution_budgets"]
    assert budgets["qa_concurrency"] == 4 and budgets["turn_timeout"] == 3660
    return reader, state, batch


def main():
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    assert not (HERE / "terminal-inventory.json").exists()
    assert not api(8872, "active-work")["work"]
    reader, state, batch = current()
    items = {i["qa_id"]: i for i in state["items"]}
    assert len(items) == 1013
    assert items.keys() == {q["qa_id"] for q in state["qa_snapshot"]["rows"]}
    parent_reader = SplitRunReader(ROOT, ROOT / PARENT)
    parent = parent_reader.state()
    parent_items = {i["qa_id"]: i for i in parent["items"]}
    assert len(parent_items) == 196 and state["scope_id"] == parent["scope_id"]
    # Inherited execution records may receive Judge fields; compare answers and trace references.
    fields = (
        "answer",
        "execution_status",
        "termination_reason",
        "attachments",
        "predicted_media",
        "scope_id",
        "tool_trace",
        "model_call_metrics",
        "trace_quarantine",
    )
    for qa, old in parent_items.items():
        for field in fields:
            assert identity(old.get(field)) == identity(items[qa].get(field)), (qa, field)
        for old_turn, new_turn in zip(
            old.get("runtime_turns") or [], items[qa].get("runtime_turns") or [], strict=True
        ):
            for field in (
                "answer",
                "turn_id",
                "conversation_id",
                "status",
                "termination_reason",
                "attachments",
                "predicted_media",
                "trace",
                "debug_trace",
                "trace_bundle",
                "agent2_trace",
            ):
                assert identity(old_turn.get(field)) == identity(new_turn.get(field)), (qa, field)
    markers = {qa: identity(i["trace_quarantine"]) for qa, i in items.items() if i.get("trace_quarantine")}
    assert len(markers) == 18
    assert markers == {
        qa: identity(i["trace_quarantine"]) for qa, i in parent_items.items() if i.get("trace_quarantine")
    }
    scores = state.get("scoring_results") or {}
    valid = {k: v for k, v in scores.items() if v.get("batch_id") == BATCH and valid_result(v)}
    old_valid = {
        k: v
        for k, v in parent.get("scoring_results", {}).items()
        if v.get("batch_id") == BATCH and valid_result(v)
    }
    assert parent_reader.reader.fingerprint(old_valid) == reader.reader.fingerprint(
        {k: valid[k] for k in old_valid}
    )
    targets = scoring_targets(state, batch, "missing")
    inventory = []
    for qa, item in items.items():
        turns = item.get("runtime_turns") or [item]
        for idx, turn in enumerate(turns):
            score = scores.get(result_key(BATCH, qa, idx)) or {}
            if score:
                assert score["answer_fingerprint"] == answer_fingerprint(turn), qa
        score = scores.get(result_key(BATCH, qa, 0)) or {}
        inventory.append(
            {
                "qa_id": qa,
                "question": item.get("question"),
                **{
                    k: item.get(k)
                    for k in (
                        "execution_status",
                        "agent_status",
                        "termination_reason",
                        "judge_status",
                        "error",
                    )
                },
                "answer_chars": len(item.get("answer") or ""),
                "answer_sha256": hashlib.sha256((item.get("answer") or "").encode()).hexdigest(),
                "turn_ids": [t.get("turn_id") for t in turns],
                "quarantined": qa in markers,
                "scoring_status": score.get("status"),
                "scoring_reason": score.get("reason"),
                "scoring_valid": valid_result(score),
                "score": score.get("score"),
            }
        )
    with sqlite3.connect(
        "file:/home/realmagic/xy/atm-bench-20261110/product-data/sentrix.db?mode=ro", uri=True
    ) as db:
        db.row_factory = sqlite3.Row
        active = {
            t: [
                dict(r)
                for r in db.execute(
                    f"select * from {t} where status in ('pending','running','processing','queued')"
                )
            ]
            for t in ("import_jobs", "video_jobs")
        }
        active["agent_turns"] = [
            dict(r)
            for r in db.execute(
                "select id,status,scope_id from agent_turns where status in ('pending','running')"
            )
        ]
        active["pipeline_receipts"] = [
            dict(r)
            for r in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
        active["postprocess_count"] = db.execute(
            "select count(*) from scope_postprocess where processing_generation is not null"
        ).fetchone()[0]
    result = {
        "run_id": RUN,
        "status": state["status"],
        "scope_id": state["scope_id"],
        "read_revision": reader.revision,
        "batch_id": BATCH,
        "manifest_sha256": hashlib.sha256((ROOT / RUN / "split-commit.json").read_bytes()).hexdigest(),
        "qa_count": len(items),
        "inherited_items_unchanged": len(parent_items),
        "inherited_valid_scores_unchanged": len(old_valid),
        "quarantine_markers": markers,
        "quarantined_count": len(markers),
        "execution_counts": dict(Counter(i.get("execution_status") for i in items.values())),
        "termination_counts": dict(Counter(i.get("termination_reason") for i in items.values())),
        "valid_atm_scores": len(valid),
        "valid_score_keys": list(valid),
        "valid_receipts_sha256": reader.reader.fingerprint(valid),
        "all_items_sha256": reader.reader.fingerprint(identity(state["items"])),
        "missing_targets": targets,
        "missing_score_targets": sum(not t.get("diagnostics_only") for t in targets),
        "diagnostics_only_targets": sum(bool(t.get("diagnostics_only")) for t in targets),
        "frozen_batch": {
            k: batch.get(k) for k in ("batch_id", "rule_version", "judge", "qa_definition_sha256")
        },
        "runtime_snapshot": state.get("runtime_snapshot"),
        "product_activity": active,
        "resources": resources(),
        "items": inventory,
        "scoring": reader.overview()["summary"]["scoring"]["profiles"]["atm_qs_v1"],
        "finished_at": datetime.now().astimezone().isoformat(),
        "validator_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    assert SplitRunReader(ROOT, ROOT / RUN).revision == reader.revision
    save("terminal-inventory.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "run_id",
                    "read_revision",
                    "qa_count",
                    "execution_counts",
                    "termination_counts",
                    "valid_atm_scores",
                    "missing_score_targets",
                    "diagnostics_only_targets",
                    "quarantined_count",
                    "product_activity",
                    "resources",
                    "validator_peak_rss_kib",
                )
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
