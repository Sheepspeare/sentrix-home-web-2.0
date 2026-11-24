#!/usr/bin/env python3
"""Score saved Oracle answers with Sentrix's frozen ATM scoring protocol."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT / "apps/photobench/src"))

from photobench.application.run_execution.judge import JudgeRunner
from photobench.domain.evaluation.atm.scoring import _prediction_list_items, list_jaccard_score
from photobench.domain.evaluation.contract import result_record, validate_evaluation
from photobench.infrastructure.http.client import HttpxJsonClient

from qwen38_oracle import digest, now, write_json


SOURCE_FILES = [
    "apps/photobench/src/photobench/application/run_execution/judge.py",
    "apps/photobench/src/photobench/application/run_execution/typed_scoring.py",
    "apps/photobench/src/photobench/domain/evaluation/atm/scoring.py",
    "apps/photobench/src/photobench/domain/evaluation/atm/normalizer.py",
    "apps/photobench/src/photobench/domain/evaluation/atm/constants.py",
    "apps/photobench/src/photobench/domain/evaluation/contract.py",
    "apps/photobench/src/photobench/domain/runs/judge.py",
    "apps/photobench/src/photobench/infrastructure/http/client.py",
]


def score_one(judge, qa, answer):
    if qa["evaluation"]["scorer"] == "atm_list":
        # Oracle's protocol delivers source IDs as text, not product attachments.
        # Use the identical production ATM scorer on that actual delivered text.
        # Keep all predictions, including wrong IDs; never fill from evidence/GT.
        return result_record(
            qa["evaluation"], list_jaccard_score(qa["answer"], answer),
            reason="ATM-Bench list_jaccard_score on Oracle's delivered ID answer",
            official_prediction=answer, predicted_ids=_prediction_list_items(answer),
            delivery_protocol="oracle_text_evidence_ids",
            attempts=[], call_observations=[],
        )
    return judge.judge(
        qa["question"], qa["answer"], answer, gt_row=qa,
        conversation=[{"message": qa["question"], "answer": answer}],
    )


def summarize(records, selected, elapsed):
    valid = [r for r in records if r["result"].get("score") is not None]
    total = sum((Decimal(str(r["result"]["normalized_score"])) for r in valid), Decimal(0))
    auxiliary = [r["result"].get("number_aux") for r in records
                 if r["result"].get("scorer") == "atm_number"]
    calls = [a for r in records for a in r["result"].get("attempts", [])]
    raw_responses = [a["raw_response"] for a in calls if a.get("raw_response")]
    observations = [a for r in records for a in r["result"].get("call_observations", [])]
    return {
        "updated_at": now(), "selected": selected, "finished": len(records),
        "valid_primary_scores": len(valid), "failed_primary": len(records) - len(valid),
        "score_sum": str(total),
        "all_selected_missing_as_zero_percent": str(total / selected * 100) if selected else None,
        "valid_only_mean_percent": str(total / len(valid) * 100) if valid else None,
        "numeric_auxiliary_finished": len(auxiliary),
        "numeric_auxiliary_failed": sum(a is None or a.get("score") is None for a in auxiliary),
        "numeric_auxiliary_disagreements": sum(bool(r["result"].get("number_aux_disagrees")) for r in records),
        "judge_logical_requests": len(calls), "judge_http_attempts": len(observations),
        "judge_raw_response_models": dict(Counter(r.get("model", "missing") for r in raw_responses)),
        "judge_usage": {k: sum(r.get("usage", {}).get(k, 0) or 0 for r in raw_responses)
                        for k in ["prompt_tokens", "completion_tokens", "total_tokens"]},
        "wall_seconds": round(elapsed, 3),
    }


def make_judge(protocol, key, http=None):
    return JudgeRunner(
        http=http or HttpxJsonClient(), judge_url=protocol["judge_url"],
        judge_model=protocol["judge_model"], judge_api_key=key,
        evaluation_options=protocol["/evaluation_options"],
        evaluation_prompts=protocol["/evaluation_prompts"],
    )


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--credential", type=Path, required=True)
    p.add_argument("--label", default="scores")
    p.add_argument("--seed-label")
    p.add_argument("--ids")
    p.add_argument("--workers", type=int, default=2)
    args = p.parse_args()
    if not 1 <= args.workers <= 4:
        p.error("Use 1 to 4 workers")
    preflight = json.loads((args.audit / "preflight.json").read_text())
    protocol_path = args.audit / "judge-protocol-snapshot.json"
    protocol = json.loads(protocol_path.read_text())
    qa_path = Path(preflight["sentrix_qa"])
    if digest(qa_path) != preflight["sentrix_qa_sha256"]:
        raise ValueError("Reference dataset changed since generation preflight")
    qas = [json.loads(line) for line in qa_path.read_text().splitlines()]
    answers_path = args.audit / "full/answers.jsonl"
    answers = {r["id"]: r for r in map(json.loads, answers_path.read_text().splitlines())}
    if len(qas) != 1013 or {q["source_qa_id"] for q in qas} != set(answers):
        raise ValueError("Expected all 1013 current references and Oracle answers")
    for qa in qas:
        validate_evaluation(qa)
        if not answers[qa["source_qa_id"]]["success"]:
            raise ValueError("Generation must be accepted before scoring")
    ids = set(args.ids.split(",")) if args.ids else set(answers)
    selected = [q for q in qas if q["source_qa_id"] in ids]
    if len(ids) != len(selected):
        raise ValueError("Unknown QA selection")
    destination = args.audit / args.label
    destination.mkdir(exist_ok=True)
    manifest = {
        "runner_sha256": digest(__file__), "answers_sha256": digest(answers_path),
        "protocol_sha256": digest(protocol_path), "qa_sha256": digest(qa_path),
        "scorer_source_sha256": {s: digest(CODE_ROOT / s) for s in SOURCE_FILES},
        "ids": [q["source_qa_id"] for q in selected], "workers": args.workers,
        "judge_model": protocol["judge_model"],
        "evaluation_options": protocol["/evaluation_options"],
        "list_delivery_protocol": "oracle_text_evidence_ids",
        "seed_label": args.seed_label,
    }
    target = destination / "manifest.json"
    if target.exists() and json.loads(target.read_text()) != manifest:
        raise ValueError("Scoring manifest changed; use a new output label")
    write_json(target, manifest)
    results = destination / "items"
    results.mkdir(exist_ok=True)
    if args.seed_label:
        seed = args.audit / args.seed_label
        seed_manifest = json.loads((seed / "manifest.json").read_text())
        for field in ["runner_sha256", "answers_sha256", "protocol_sha256", "qa_sha256", "scorer_source_sha256"]:
            if seed_manifest[field] != manifest[field]:
                raise ValueError(f"Seed scoring protocol changed: {field}")
        for qid in set(seed_manifest["ids"]) & ids:
            path = seed / "items" / (qid + ".json")
            record = json.loads(path.read_text())
            auxiliary = record["result"].get("number_aux", {})
            if record["result"].get("score") is not None and auxiliary.get("judge_status") != "failed":
                target = results / path.name
                if not target.exists():
                    write_json(target, record)
    key = json.loads(args.credential.read_text())["api_key"]
    judge = make_judge(protocol, key)

    def execute(qa):
        qid = qa["source_qa_id"]
        path = results / (qid + ".json")
        if path.exists():
            return json.loads(path.read_text())
        started = time.monotonic()
        result = score_one(judge, qa, answers[qid]["answer"])
        record = {"id": qid, "qtype": qa["source_qa"]["qtype"],
                  "answer": answers[qid]["answer"], "reference": qa["answer"],
                  "result": result, "finished_at": now(),
                  "seconds": round(time.monotonic() - started, 3)}
        write_json(path, record)
        return record

    started = time.monotonic()
    records = []
    write_json(destination / "summary.json", {**summarize(records, len(selected), 0), "status": "running"})
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(execute, q) for q in selected]):
            records.append(future.result())
            summary = summarize(records, len(selected), time.monotonic() - started)
            write_json(destination / "summary.json", {**summary, "status": "running"})
            print(json.dumps({k: summary[k] for k in ["finished", "valid_primary_scores", "failed_primary", "numeric_auxiliary_failed"]}), flush=True)
    plan = {q["id"]: q for q in preflight["questions"]}
    by_id = {r["id"]: r for r in records}
    groups = defaultdict(list)
    for r in records:
        groups["type:" + r["qtype"]].append(r)
        groups["modality:" + "+".join(plan[r["id"]]["modalities"])].append(r)
    summary = summarize(records, len(selected), time.monotonic() - started)
    summary.update(status="finished", by_group={
        k: summarize(v, len(v), 0) for k, v in sorted(groups.items())
    })
    with (destination / "scores.csv").open("w", encoding="utf-8", newline="") as f:
        fields = ["id", "qtype", "modality", "score", "normalized_score", "judge_status", "number_aux_score", "answer", "reference", "reason"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for qa in selected:
            qid = qa["source_qa_id"]
            r = by_id[qid]
            score = r["result"]
            writer.writerow({"id": qid, "qtype": r["qtype"],
                             "modality": "+".join(plan[qid]["modalities"]),
                             **{k: score.get(k) for k in ["score", "normalized_score", "judge_status", "reason"]},
                             "number_aux_score": score.get("number_aux", {}).get("score"),
                             "answer": r["answer"], "reference": r["reference"]})
    write_json(destination / "summary.json", summary)
    successful = summary["failed_primary"] == 0 and summary["numeric_auxiliary_failed"] == 0
    write_json(destination / "terminal-event.json", {
        "status": "completed" if successful else "needs_review", "summary": summary,
        "artifact_path": str(destination),
    })
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if successful else 2


if __name__ == "__main__":
    sys.exit(main())
