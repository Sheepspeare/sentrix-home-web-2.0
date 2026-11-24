#!/usr/bin/env python3
"""Run ATM's official raw-evidence oracle through Qwen's compatible API.

Import evidence/prompt builders from a separately downloaded official checkout.
Keep answers and experiment state outside that checkout and the product database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import requests

PARAMETERS = {
    "model": "qwen3.8-flash",
    "enable_thinking": True,
    "reasoning_effort": "high",
    "max_completion_tokens": 65536,
    "temperature": 0.2,
    "stream": False,
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


class Oracle:
    def __init__(self, root, output):
        self.root = root.resolve()
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(self.root))
        self.official = importlib.import_module(
            "memqa.qa_agent_baselines.oracle.oracle_baseline"
        )
        self.qas = self.official.load_qa_list(
            self.official.load_json(self.root / "data/atm-bench/atm-bench.json")
        )
        self.emails = self.official.load_email_index(
            self.root / "data/raw_memory/email/emails.json"
        )
        self.images = self.official.build_batch_index(
            self.official.load_json(
                self.root / "output/image/qwen3vl2b/batch_results.json"
            ),
            "image_path",
        )
        self.videos = self.official.build_batch_index(
            self.official.load_json(
                self.root / "output/video/qwen3vl2b/batch_results.json"
            ),
            "video_path",
        )
        self.image_root = self.root / "data/raw_memory/image"
        self.video_root = self.root / "data/raw_memory/video"
        self.temporary = self.output / "temporary-frames"
        self.temporary.mkdir(exist_ok=True)
        self.local = threading.local()
        # Scope temporary-directory tracking to the imported oracle module.
        self.official.tempfile = SimpleNamespace(mkdtemp=self.make_temp)

    def make_temp(self):
        path = tempfile.mkdtemp(prefix="frames-", dir=self.temporary)
        self.local.directories.append(path)
        return path

    def evidence(self, qa):
        evidence = []
        for eid in self.official.extract_evidence_ids(qa):
            if self.official.classify_evidence_id(eid) == "email":
                if eid not in self.emails:
                    raise ValueError(f"Missing email {eid}")
                evidence.append({"id": eid, "type": "email"})
            else:
                kind, path = self.official.resolve_media_evidence(
                    eid, self.image_root, self.video_root
                )
                if path is None or kind is None:
                    raise ValueError(f"Missing media {eid}")
                evidence.append({"id": eid, "type": kind, "path": str(path)})
        if not evidence:
            raise ValueError(f"No evidence: {qa['id']}")
        return evidence

    def build_input(self, qa):
        # Only these two QA fields reach the official prompt builders.
        question = qa["question"]
        ids = self.official.extract_evidence_ids(qa)
        self.local.directories = []
        try:
            items, chunks = self.official.collect_multimodal_evidence(
                ids, self.emails, self.image_root, self.video_root,
                8, 32, "uniform", self.images, self.videos,
            )
            if len(items) + len(chunks) != len(ids):
                raise ValueError("Evidence builder omitted an evidence item")
            if any(not item["images_b64"] for item in items):
                raise ValueError("Evidence media has no image/frame")
            messages = (
                self.official.build_multimodal_messages(question, items, chunks)
                if items else self.official.build_text_messages(question, chunks)
            )
            summary = {
                "question": question,
                "evidence_ids": ids,
                "media": [
                    {"id": item["id"], "type": item["type"],
                     "frames": len(item["images_b64"]),
                     "metadata": item.get("metadata", {})}
                    for item in items
                ],
                "email_count": len(chunks),
                "messages_sha256": json_hash(messages),
                "image_or_frame_count": sum(len(i["images_b64"]) for i in items),
            }
            return messages, summary
        finally:
            for path in self.local.directories:
                shutil.rmtree(path)

    def preflight(self, sentrix_qa):
        current = {
            row["source_qa_id"]: row
            for row in map(json.loads, sentrix_qa.read_text().splitlines())
        }
        if len(self.qas) != 1013 or set(current) != {q["id"] for q in self.qas}:
            raise ValueError("Expected the same full 1013 UUIDs in both datasets")
        rows, files, changed_answers = [], {}, []
        for index, qa in enumerate(self.qas):
            row = current[qa["id"]]
            if row["question"] != qa["question"]:
                raise ValueError(f"Question mismatch: {qa['id']}")
            if row["answer"] != qa["answer"]:
                changed_answers.append(qa["id"])
            evidence = self.evidence(qa)
            for item in evidence:
                if "path" in item:
                    files[item["path"]] = None
            rows.append({"index": index, "id": qa["id"], "qtype": qa["qtype"],
                         "modalities": sorted({e["type"] for e in evidence}),
                         "evidence": evidence})
        # Hash the actual evidence, not merely the QA list naming it.
        def describe(path):
            p = Path(path)
            stat = p.stat()
            return path, {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                          "sha256": digest(p)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            files = dict(pool.map(describe, files))
        sources = [
            "data/atm-bench/atm-bench.json", "data/raw_memory/email/emails.json",
            "output/image/qwen3vl2b/batch_results.json",
            "output/video/qwen3vl2b/batch_results.json",
            "memqa/qa_agent_baselines/oracle/oracle_baseline.py",
            "memqa/qa_agent_baselines/oracle/config.py",
            "memqa/mem_processor/video/utils.py", "memqa/global_config.py",
        ]
        commit = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()
        plan = {
            "created_at": now(), "official_root": str(self.root),
            "official_commit": commit, "official_origin":
            "https://github.com/JingbiaoMei/ATM-Bench.git",
            "source_sha256": {p: digest(self.root / p) for p in sources},
            "sentrix_qa": str(sentrix_qa), "sentrix_qa_sha256": digest(sentrix_qa),
            "reference_answer_differences": changed_answers,
            "parameters": PARAMETERS, "effective_effort_per_official_docs": "xhigh",
            "media_source": "raw", "num_frames_per_video": 8,
            "max_total_video_frames": 32, "frame_strategy": "uniform",
            "max_evidence_items": None, "qa_count": len(rows),
            "qtypes": dict(Counter(q["qtype"] for q in rows)),
            "modalities": dict(Counter("+".join(q["modalities"]) for q in rows)),
            "media_files": files, "questions": rows,
        }
        write_json(self.output / "preflight.json", plan)
        return plan


def call_api(endpoint, api_key, messages):
    attempts = []
    for attempt in range(1, 4):
        started = time.monotonic()
        try:
            response = requests.post(
                endpoint + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key},
                json={**PARAMETERS, "messages": messages}, timeout=(30, 1800),
            )
            attempts.append({"attempt": attempt, "status": response.status_code,
                             "seconds": round(time.monotonic() - started, 3)})
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(5 * attempt)
                    continue
            if response.status_code != 200:
                return {"error": f"HTTP {response.status_code}", "attempts": attempts}
            result = response.json()
            return {"response": result, "attempts": attempts}
        except requests.RequestException as exc:
            # Exception text may contain provider URLs. Persist the type only.
            attempts.append({"attempt": attempt, "error": type(exc).__name__,
                             "seconds": round(time.monotonic() - started, 3)})
            if attempt < 3:
                time.sleep(5 * attempt)
    return {"error": "Transport attempts exhausted", "attempts": attempts}


def run(args, oracle, plan):
    if plan["parameters"] != PARAMETERS or plan["official_root"] != str(oracle.root):
        raise ValueError("Preflight configuration mismatch")
    for p, sha in plan["source_sha256"].items():
        if digest(oracle.root / p) != sha:
            raise ValueError(f"Source changed after preflight: {p}")
    if digest(Path(plan["sentrix_qa"])) != plan["sentrix_qa_sha256"]:
        raise ValueError("Current reference dataset changed after preflight")
    for p, desc in plan["media_files"].items():
        stat = Path(p).stat()
        if (stat.st_size, stat.st_mtime_ns) != (desc["bytes"], desc["mtime_ns"]):
            raise ValueError(f"Evidence changed after preflight: {p}")
    private = dict(csv.reader(args.api_key_csv.open(encoding="utf-8-sig")))
    endpoint = private["openAiCompatible"].rstrip("/")
    key = private["apiKey"]
    ids = set(args.ids.split(",")) if args.ids else {q["id"] for q in oracle.qas}
    selected = [q for q in oracle.qas if q["id"] in ids]
    if len(selected) != len(ids):
        raise ValueError("Unknown or duplicate question selection")
    destination = oracle.output / args.label
    destination.mkdir(exist_ok=True)
    manifest = {
        "parameters": PARAMETERS, "preflight_sha256": digest(oracle.output / "preflight.json"),
        "runner_sha256": digest(__file__), "ids": [q["id"] for q in selected],
        "workers": args.workers, "request_timeout_seconds": 1800,
        "transport_max_attempts": 3,
        "seed_label": args.seed_label,
    }
    manifest_path = destination / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("Existing run manifest differs; use a new label")
    write_json(manifest_path, manifest)
    responses = destination / "responses"
    responses.mkdir(exist_ok=True)
    if args.seed_label:
        if args.seed_label == args.label:
            raise ValueError("Seed label must refer to another run")
        source = oracle.output / args.seed_label
        seed = json.loads((source / "manifest.json").read_text())
        for field in ["parameters", "preflight_sha256", "runner_sha256"]:
            if seed[field] != manifest[field]:
                raise ValueError(f"Seed differs in {field}")
        for qid in set(seed["ids"]) & ids:
            source_file = source / "responses" / (qid + ".json")
            record = json.loads(source_file.read_text())
            target = responses / source_file.name
            if record["success"] and not target.exists():
                shutil.copy2(source_file, target)

    def execute(qa):
        path = responses / (qa["id"] + ".json")
        if path.exists():
            return json.loads(path.read_text())
        start = time.monotonic()
        record = {"id": qa["id"], "qtype": qa["qtype"], "started_at": now()}
        try:
            messages, evidence = oracle.build_input(qa)
            record["input"] = evidence
            result = call_api(endpoint, key, messages)
            record.update(result)
            if "response" in result:
                data = result["response"]
                choice = data["choices"][0]
                message = choice["message"]
                record.update({
                    "model": data.get("model"), "finish_reason": choice.get("finish_reason"),
                    "answer": message.get("content") or "", "usage": data.get("usage", {}),
                    "reasoning_characters": len(message.get("reasoning_content") or ""),
                })
                record["success"] = (
                    record["model"] == PARAMETERS["model"]
                    and record["finish_reason"] == "stop" and bool(record["answer"].strip())
                )
            else:
                record["success"] = False
        except Exception as exc:
            record.update({"success": False, "error": type(exc).__name__})
        record.update({"finished_at": now(), "seconds": round(time.monotonic() - start, 3)})
        write_json(path, record)
        return record

    records = []
    started = time.monotonic()
    def report(terminal=False):
        summary = {
            "updated_at": now(), "status": "finished" if terminal else "running",
            "selected": len(selected), "finished": len(records),
            "successful": sum(r.get("success", False) for r in records),
            "failed": sum(not r.get("success", False) for r in records),
            "length_truncated": sum(r.get("finish_reason") == "length" for r in records),
            "reasoning_returned": sum(r.get("reasoning_characters", 0) > 0 for r in records),
            "models": dict(Counter(r.get("model", "missing") for r in records)),
            "wall_seconds": round(time.monotonic() - started, 3),
            "usage": {k: sum(r.get("usage", {}).get(k, 0) or 0 for r in records)
                      for k in ["prompt_tokens", "completion_tokens", "total_tokens"]},
            "reasoning_tokens": sum(
                r.get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0
                for r in records
            ),
        }
        write_json(destination / "summary.json", summary)
        return summary
    report()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(execute, q) for q in selected]):
            records.append(future.result())
            summary = report()
            print(json.dumps({k: summary[k] for k in ["finished", "successful", "failed"]}), flush=True)
    by_id = {r["id"]: r for r in records}
    with (destination / "answers.jsonl").open("w", encoding="utf-8") as f:
        for qa in selected:
            record = by_id[qa["id"]]
            f.write(json.dumps({"id": qa["id"], "answer": record.get("answer", ""),
                                "success": record["success"]}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    summary = report(terminal=True)
    write_json(destination / "terminal-event.json", {
        "status": "completed" if summary["failed"] == 0 else "needs_review",
        "summary": summary, "artifact_path": str(destination),
    })
    print(json.dumps(summary), flush=True)
    return 0 if summary["failed"] == 0 else 2


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["preflight", "run"])
    p.add_argument("--official-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--sentrix-qa", type=Path)
    p.add_argument("--api-key-csv", type=Path)
    p.add_argument("--ids")
    p.add_argument("--label", default="full")
    p.add_argument("--seed-label")
    p.add_argument("--workers", type=int, default=2)
    args = p.parse_args()
    if not 1 <= args.workers <= 4:
        p.error("Use 1 to 4 workers")
    oracle = Oracle(args.official_root, args.output)
    if args.action == "preflight":
        if not args.sentrix_qa:
            p.error("preflight requires --sentrix-qa")
        plan = oracle.preflight(args.sentrix_qa)
        print(json.dumps({k: plan[k] for k in ["qa_count", "qtypes", "modalities"]}))
        return 0
    if not args.api_key_csv:
        p.error("run requires --api-key-csv")
    return run(args, oracle, json.loads((args.output / "preflight.json").read_text()))


if __name__ == "__main__":
    sys.exit(main())
