"""Read three accepted ATM runs; write compact scores without reading trace bodies."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import resource
import sys
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
APP = BASE / "doubao-persist-projection-v2/candidate/apps/photobench/src"
sys.path.insert(0, str(APP))

from photobench.domain.evaluation.profiles import (  # noqa: E402
    evaluation_turn,
    result_key,
    valid_result,
)
from photobench.infrastructure.results.split_json import MANIFEST  # noqa: E402
from photobench.infrastructure.results.split_store import SplitRunReader  # noqa: E402

BATCH = "score-dd0413c8aa869b1c84626de7"
QA_SHA = "a885d405b9d14e17bc0c60803fbc4153c18eb4a2adb44361973ebe214424ddd0"
RUNS = {
    "qwen37": {
        "model": "qwen3.7-plus",
        "root": BASE / "cloud-results",
        "run_id": "20261119-235632-2026.11.17-atm-sources-addresses-current-resume-0bbacb",
        "revision": "ea78b2c097204a328df0f376e9db1112",
        "valid_count": 1012,
    },
    "q4km": {
        "model": "qwen3.5-4b-q4_k_m",
        "root": Path("/home/realmagic/xy/atm-bench-20261110/results"),
        "run_id": "20261120-162112-2026.11.17-atm-sources-addresses-current-resume-93efab",
        "revision": "28381826d3174640bd2d07c50f3eb9a4",
        "valid_count": 1012,
    },
    "doubao": {
        "model": "doubao-seed-2.1-lite",
        "root": BASE / "cloud-results",
        "run_id": "20261122-114200-2026.11.17-atm-sources-addresses-current-resume-9763a1",
        "revision": "24b94722a11249a7a67e99cbbbbb5a30",
        "valid_count": 852,
    },
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mean(values):
    return math.fsum(values) / len(values) if values else None


def aggregate(rows, ids):
    valid = [rows[qa]["score"] for qa in ids if rows[qa]["score"] is not None]
    return {
        "qa_count": len(ids),
        "valid_count": len(valid),
        "coverage": len(valid) / len(ids) if ids else None,
        "valid_mean": mean(valid),
    }


def read_run(spec, frozen):
    directory = spec["root"] / spec["run_id"]
    before = sha(directory / MANIFEST)
    reader = SplitRunReader(spec["root"], directory)
    assert reader.revision == spec["revision"]
    state = reader.state()
    assert not state.get("fatal_error") and not state.get("current_phase")
    assert state["status"] in {"completed", "completed_with_errors"}
    batch = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
    assert batch["profile_id"] == "atm_qs_v1"
    assert batch["qa_definition_sha256"] == QA_SHA
    assert batch["config"] == frozen["scoring_configs"]["atm_qs_v1"]
    assert batch["rule_version"] == "scoring-2026.11.12-v5"
    assert batch["judge"]["model"] == "doubao-seed-2.1-lite"
    qa_rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    items = {i["qa_id"]: i for i in state["items"]}
    assert items.keys() == qa_rows.keys()
    assert len(items) == len(state["items"]) == state["qa_count"] == 1013
    receipts = state["scoring_results"]
    rows = {}
    for qa, item in items.items():
        turn = evaluation_turn(qa_rows[qa])
        key = result_key(BATCH, qa, turn)
        receipt = receipts.get(key) or {}
        if receipt:
            assert receipt["qa_id"] == qa and receipt["batch_id"] == BATCH
            assert receipt["turn_index"] == turn and receipt["profile_id"] == "atm_qs_v1"
        rows[qa] = {
            "qa_id": qa,
            "answer_format": (qa_rows[qa].get("evaluation") or {}).get("answer_format"),
            "evaluation_turn": turn,
            "execution_status": item.get("execution_status"),
            "agent_status": item.get("agent_status"),
            "termination_reason": item.get("termination_reason"),
            "score_status": receipt.get("status", "not_collected"),
            "score": receipt["score"] if valid_result(receipt) else None,
            "purpose": receipt.get("purpose"),
        }
    summary = {
        "model": spec["model"],
        "root": str(spec["root"]),
        "run_id": spec["run_id"],
        "read_revision": reader.revision,
        "manifest_sha256": before,
        "status": state["status"],
        "runtime_model": (state.get("runtime_snapshot") or {}).get("endpoint_model"),
        **aggregate(rows, list(rows)),
        "execution_counts": dict(Counter(r["execution_status"] for r in rows.values())),
        "agent_status_counts": dict(Counter(str(r["agent_status"]) for r in rows.values())),
        "termination_counts": dict(Counter(str(r["termination_reason"]) for r in rows.values())),
        "score_status_counts": dict(Counter(r["score_status"] for r in rows.values())),
        "missing_score_ids": sorted(qa for qa, row in rows.items() if row["score"] is None),
        "model_error_with_valid_score_ids": sorted(
            qa
            for qa, row in rows.items()
            if row["termination_reason"] == "model_error" and row["score"] is not None
        ),
        "by_answer_format": {
            fmt: aggregate(rows, [qa for qa, row in rows.items() if row["answer_format"] == fmt])
            for fmt in sorted({str(r["answer_format"]) for r in rows.values()})
        },
    }
    assert summary["valid_count"] == spec["valid_count"]
    assert sha(directory / MANIFEST) == before
    reader.reader.close()
    return summary, rows


def compare(all_rows):
    names = list(all_rows)
    qa_ids = set(all_rows[names[0]])
    assert all(set(rows) == qa_ids for rows in all_rows.values())
    assert all(len({rows[qa]["answer_format"] for rows in all_rows.values()}) == 1 for qa in qa_ids)
    valid_ids = {
        name: {qa for qa, row in rows.items() if row["score"] is not None} for name, rows in all_rows.items()
    }
    common = sorted(set.intersection(*valid_ids.values()))
    formats = sorted({str(row["answer_format"]) for row in all_rows[names[0]].values()})
    pairs = []
    for a, b in combinations(names, 2):
        ids = sorted(valid_ids[a] & valid_ids[b])
        diffs = [all_rows[a][qa]["score"] - all_rows[b][qa]["score"] for qa in ids]
        ties = sum(math.isclose(d, 0, abs_tol=1e-12) for d in diffs)
        pairs.append(
            {
                "a": a,
                "b": b,
                "common_valid_count": len(ids),
                "a_mean": mean([all_rows[a][qa]["score"] for qa in ids]),
                "b_mean": mean([all_rows[b][qa]["score"] for qa in ids]),
                "a_minus_b": mean(diffs),
                "a_higher": sum(d > 1e-12 for d in diffs),
                "b_higher": sum(d < -1e-12 for d in diffs),
                "ties": ties,
            }
        )
    return {
        "common_valid_count": len(common),
        "common_valid_qa_ids": common,
        "common_valid_means": {name: aggregate(rows, common) for name, rows in all_rows.items()},
        "common_by_answer_format": {
            fmt: {
                name: aggregate(rows, [qa for qa in common if rows[qa]["answer_format"] == fmt])
                for name, rows in all_rows.items()
            }
            for fmt in formats
        },
        "pairwise": pairs,
    }


def main(output):
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    assert not (output / "comparison.json").exists()
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    mapping = (BASE / "active-runs.json").read_bytes()
    active = json.loads(mapping)
    summaries, all_rows = {}, {}
    for name, spec in RUNS.items():
        assert active[name]["run_id"] == spec["run_id"]
        summaries[name], all_rows[name] = read_run(spec, frozen)
        print(json.dumps({"model": name, **aggregate(all_rows[name], list(all_rows[name]))}), flush=True)
    for name, spec in RUNS.items():
        assert sha(spec["root"] / spec["run_id"] / MANIFEST) == summaries[name]["manifest_sha256"]
    assert (BASE / "active-runs.json").read_bytes() == mapping
    result = {
        "created_at": datetime.now().astimezone().isoformat(),
        "batch_id": BATCH,
        "qa_definition_sha256": QA_SHA,
        "rule_version": "scoring-2026.11.12-v5",
        "scoring_config_sha256": hashlib.sha256(
            json.dumps(frozen["scoring_configs"]["atm_qs_v1"], sort_keys=True).encode()
        ).hexdigest(),
        "runs": summaries,
        **compare(all_rows),
        "source_manifests_and_active_mapping_unchanged": True,
        "agent_calls": 0,
        "judge_calls": 0,
        "trace_bodies_read": False,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "comparison.json").open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    common = set(result["common_valid_qa_ids"])
    with (output / "qa-scores.csv").open("x", newline="") as stream:
        fields = ["qa_id", "answer_format", "common_valid"] + [
            f"{name}_{field}"
            for name in RUNS
            for field in ("score", "execution_status", "agent_status", "termination_reason", "score_status")
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for qa in sorted(all_rows["qwen37"]):
            row = {
                "qa_id": qa,
                "answer_format": all_rows["qwen37"][qa]["answer_format"],
                "common_valid": qa in common,
            }
            for name, rows in all_rows.items():
                for field in (
                    "score",
                    "execution_status",
                    "agent_status",
                    "termination_reason",
                    "score_status",
                ):
                    row[f"{name}_{field}"] = rows[qa][field]
            writer.writerow(row)
    print(json.dumps({"common_valid_count": result["common_valid_count"], "pairwise": result["pairwise"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
