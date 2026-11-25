"""Freeze diagnostic questions from local historical snapshots; never calls a service."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

SEED = "atm-cap-002-005-20261125-v1"
REPRESENTATIVES = {
    "atm-bcf1e962-4d23-481c-998a-35a54bb6e420": "Q052",
    "atm-cc7ac922-e059-4bf7-b789-fcda05cdfa7e": "Q117",
    "atm-fadf2286-8319-4a22-bd20-724a3471afa2": "Q528",
    "atm-d2beb429-0a4e-4f20-a3f9-2047937630fe": "Q539",
}
QUOTAS = {
    ("图片", "number"): 12,
    ("图片", "open_end"): 12,
    ("图片", "list_recall"): 12,
    ("视频", "number"): 4,
    ("视频", "open_end"): 4,
    ("视频", "list_recall"): 4,
    ("邮件", "number"): 6,
    ("邮件", "open_end"): 5,
    ("邮件", "list_recall"): 1,
}
RUNS = ("93efab", "0bbacb", "7cfe20")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def rank(qa_id, purpose="selection"):
    return hashlib.sha256(f"{SEED}:{purpose}:{qa_id}".encode()).hexdigest()


def select(taxonomy, histories):
    rows = {row["qa_id"]: row for row in taxonomy}
    if len(rows) != 1013 or len(taxonomy) != 1013:
        raise ValueError("expected 1013 unique taxonomy IDs")
    if not REPRESENTATIVES.keys() <= rows.keys():
        raise ValueError("missing representative IDs")
    for name, history in histories.items():
        if len(history) != 1013 or len({r["qa_id"] for r in history}) != 1013:
            raise ValueError(f"{name}: expected 1013 unique historical IDs")
        if {r["qa_id"] for r in history} != rows.keys():
            raise ValueError(f"{name}: historical IDs differ from taxonomy")
    by_run = {name: {r["qa_id"]: r for r in values} for name, values in histories.items()}
    chosen, counts = {}, Counter()

    def add(qid, reason):
        row = rows[qid]
        stratum = row["modality"], row["native_qtype"]
        if qid in chosen or counts[stratum] >= QUOTAS[stratum]:
            return False
        chosen[qid] = reason
        counts[stratum] += 1
        return True

    ordered = sorted(rows, key=rank)
    for qid in REPRESENTATIVES:
        add(qid, "requirement_representative")
    ability_sizes = Counter(r["primary"] for r in taxonomy)
    for ability in sorted(ability_sizes, key=lambda a: (ability_sizes[a], a)):
        if any(rows[q]["primary"] == ability for q in chosen):
            continue
        if not any(add(q, "ability_coverage") for q in ordered if rows[q]["primary"] == ability):
            raise ValueError(f"cannot cover ability {ability} within quotas")
    # Historical tags select diagnostic candidates; they do not establish a root cause.
    tagged = [
        q
        for q in ordered
        if any(by_run[r][q].get("termination_reason") == "step_budget_exhausted" for r in histories)
    ]
    normal = [
        q
        for q in ordered
        if all(
            (by_run[r][q].get("judge") or {}).get("normalized_score") == 1
            and by_run[r][q].get("termination_reason") != "step_budget_exhausted"
            for r in histories
        )
    ]
    for candidates, reason in ((tagged, "historical_step_tag"), (normal, "historical_full_score_control")):
        accepted = 0
        for qid in candidates:
            accepted += add(qid, reason)
            if accepted == 8:
                break
        if accepted != 8:
            raise ValueError(f"not enough candidates for {reason}")
    for qid in ordered:
        add(qid, "seeded_stratified_sample")
    if dict(counts) != QUOTAS or len(chosen) != 60:
        raise ValueError("diagnostic quotas not satisfied")
    stability = set(REPRESENTATIVES)
    for modality, target in (("图片", 12), ("视频", 4), ("邮件", 4)):
        for qid in sorted(chosen, key=lambda q: rank(q, "stability")):
            if sum(rows[q]["modality"] == modality for q in stability) >= target:
                break
            if rows[qid]["modality"] == modality:
                stability.add(qid)
    output = []
    for qid in sorted(chosen, key=lambda q: rows[q]["n"]):
        row = rows[qid]
        output.append(
            {
                "qa_id": qid,
                "current_index": row["n"] - 1,
                "historical_label": REPRESENTATIVES.get(qid),
                "modality": row["modality"],
                "qtype": row["native_qtype"],
                "ability": row["primary"],
                "question": row["question"],
                "selection_reason": chosen[qid],
                "stability_subset": qid in stability,
                "historical_step_tag_runs": [
                    r
                    for r in histories
                    if by_run[r][qid].get("termination_reason") == "step_budget_exhausted"
                ],
            }
        )
    return output, sorted(rows.keys() - chosen.keys())


def evidence(cases_dir):
    result = []
    for run in RUNS:
        for case in read(cases_dir / f"{run}-representative-cases.json"):
            calls = [t for t in case["tools"] if t["tool"] == "inspect_photo"]
            observations = [o for t in calls for o in t.get("model_observations", [])]
            known_images = [
                o.get("image_count") if o.get("image_count") is not None else o.get("images_count")
                for o in observations
            ]
            complete_images = bool(observations) and all(type(n) is int for n in known_images)
            result.append(
                {
                    "run_suffix": run,
                    "qa_id": case["qa_id"],
                    "historical_label": case["label"],
                    "turn_ids": [t["turn_id"] for t in case["turns"]],
                    "termination_reason": case["termination_reason"],
                    "inspect_requests": len(calls),
                    "inspect_tool_ok": sum(t["status"] == "ok" for t in calls),
                    "read_stored_text_requests": sum(t["tool"] == "read_photo_text" for t in case["tools"]),
                    "denials": [t for t in case["tools"] if t["status"] == "denied"],
                    "model_complete_records": sum(o.get("status") == "complete" for o in observations),
                    "image_input_count": sum(known_images) if complete_images else None,
                    "image_input_evidence": "recorded_counts"
                    if complete_images
                    else "not_available_in_compact_snapshot",
                    "requirement_incident_binding": "not_established_for_this_run",
                }
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taxonomy", required=True, type=Path)
    parser.add_argument("--history-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    paths = [args.taxonomy] + [args.history_dir / f"{r}-items-all.json" for r in RUNS]
    history = {r: read(p) for r, p in zip(RUNS, paths[1:], strict=True)}
    selected, holdout = select(read(args.taxonomy), history)
    args.output.mkdir(parents=True, exist_ok=True)
    write(args.output / "diagnostic-60.json", selected)
    write(args.output / "representatives-4.json", [r for r in selected if r["historical_label"]])
    write(args.output / "stability-20.json", [r["qa_id"] for r in selected if r["stability_subset"]])
    write(args.output / "holdout-953.json", holdout)
    write(args.output / "representative-evidence.json", evidence(args.history_dir))
    inputs = paths + [args.history_dir / f"{r}-representative-cases.json" for r in RUNS]
    write(
        args.output / "selection-receipt.json",
        {
            "seed": SEED,
            "generator": fingerprint(Path(__file__)),
            "inputs": [fingerprint(p) for p in inputs],
            "counts": {
                "diagnostic": len(selected),
                "holdout": len(holdout),
                "representatives": 4,
                "stability": 20,
            },
            "strata": [{"modality": m, "qtype": q, "count": n} for (m, q), n in QUOTAS.items()],
            "abilities": sorted({r["ability"] for r in selected}),
            "scope": "diagnostic_selection_not_population_estimate",
            "outcome_data_used": "only_historical_runs_no_current_flash_or_oracle_scores",
            "network_requests": 0,
            "model_requests": 0,
            "remote_writes": 0,
        },
    )
    print(
        json.dumps(
            {"diagnostic": len(selected), "holdout": len(holdout), "output": str(args.output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
