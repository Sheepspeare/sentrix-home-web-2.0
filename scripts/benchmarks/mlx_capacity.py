#!/usr/bin/env python3
"""Mac capacity evidence helpers; no retries of ambiguous generation/run POSTs.

Private datasets, credentials and answers stay in the supplied experiment root.
The same immutable question bytes are selected for every concurrency round.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import plistlib
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import psutil

FROZEN_487_SHA256 = "73fa4512dde072fb0df9cf45ff3ebd192192db606f9764054bde002dcbf0be47"
TERMINAL = frozenset({"completed", "completed_with_errors", "failed", "cancelled", "interrupted"})


def save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def submit_run_once(client: httpx.Client, destination: Path, payload: dict[str, Any]) -> str:
    """Persist intent before POST; any existing intent requires manual reconciliation.

    A timed-out or malformed response may follow an accepted run. Never infer
    rejection from it and never automatically submit again.
    """
    with destination.open("x") as stream:
        json.dump({"status": "submitting", "at": time.time(), "payload": payload}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    response = client.post("http://127.0.0.1:18772/api/v1/runs", json=payload, timeout=90)
    receipt = {
        "status_code": response.status_code,
        "at": time.time(),
        "payload": payload,
        "response_text": response.text,
    }
    save(destination, receipt)
    response.raise_for_status()
    data = response.json()
    run_ids = data.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or len(run_ids) != 1
        or not isinstance(run_ids[0], str)
        or not run_ids[0]
    ):
        raise ValueError("run submission response has no unique run id; reconcile without replay")
    save(destination, {**receipt, "response": data, "status": "accepted"})
    return run_ids[0]


def diagnostic_failure_categories(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count affected questions from error fields, never from QA/answer prose.

    Categories overlap (e.g. an unknown timeout); they are not a total failure
    count. Raw trace errors remain private and are not used as public labels.
    """
    patterns = {
        "oom": ("out of memory", "outofmemory", "memory allocation failed"),
        "lock_conflict": ("database is locked", "already accessed by another", "lock conflict"),
        "service_exit": ("process exited", "service exited", "server exited"),
        "timeout": ("timeout", "timed out"),
        "truncation": ("truncated", "context_window_exceeded", "context_output_insufficient"),
        "unknown": ("requestoutcomeunknown", "outcome_unknown", "request_outcome_unknown"),
    }
    counts = dict.fromkeys(patterns, 0)

    def errors(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, Mapping):
            if (
                "request_sent" in value
                and value["request_sent"] is None
                and value.get("error_category") in {"request_timeout", "service_error"}
            ):
                found.append("request_outcome_unknown")
            for key, child in value.items():
                if key in {"error", "error_type", "error_category", "termination_reason"}:
                    found.append(str(child).lower())
                elif isinstance(child, (Mapping, list)) and key not in {
                    "messages",
                    "request",
                    "response",
                    "arguments",
                    "answer",
                    "reference_answer",
                }:
                    found.extend(errors(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(errors(child))
        return found

    for item in items:
        text = "\n".join(errors(item))
        for category, needles in patterns.items():
            counts[category] += any(needle in text for needle in needles)
    return counts


def freeze_subset(source: Path, destination: Path, *, count: int = 64) -> dict[str, Any]:
    """Select whole JSONL records by seeded hash; preserve source line ordering."""
    raw = source.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    if source_sha != FROZEN_487_SHA256:
        raise ValueError("frozen 487 dataset SHA256 mismatch")
    lines = raw.splitlines(keepends=True)
    rows = [json.loads(line) for line in lines]
    if len(rows) != 487 or not 1 <= count <= 487:
        raise ValueError("expected exactly 487 complete question records")
    ranked = sorted(range(len(lines)), key=lambda i: hashlib.sha256(b"sentrix-mlx-42:" + lines[i]).digest())
    indexes = sorted(ranked[:count])
    selected = b"".join(lines[i] for i in indexes)
    if destination.exists() and destination.read_bytes() != selected:
        raise ValueError("refusing to replace a different frozen subset")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(selected)
    manifest = {
        "source_sha256": source_sha,
        "subset_sha256": hashlib.sha256(selected).hexdigest(),
        "question_count": count,
        "turn_count": sum(len(rows[i].get("conversation") or [None]) for i in indexes),
        "multiturn_questions": sum(bool(rows[i].get("conversation")) for i in indexes),
        "source_line_numbers": [i + 1 for i in indexes],
        "selection": "lowest SHA256(sentrix-mlx-42: + complete source line), original order",
        "complete_conversations_preserved": True,
    }
    save(destination.with_suffix(".manifest.json"), manifest)
    return manifest


def prometheus_values(text: str) -> dict[str, float]:
    """Keep real engine counters/gauges distinct from client concurrency."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([^ {]+)(\{[^}]*\})?\s+([^ ]+)", line)
        if not match:
            continue
        name, labels, raw = match.groups()
        if not any(
            key in name
            for key in (
                "num_requests_running",
                "num_requests_waiting",
                "request_success",
                "prompt_tokens",
                "generation_tokens",
                "time_to_first_token",
                "e2e_request_latency",
                "request_queue_time",
                "scheduler_running_requests",
                "scheduler_waiting_requests",
                "engine_steps_executed",
                "inference_requests_total",
                "inference_request_duration_seconds",
                "inference_ttft_seconds",
                "completion_tokens_total",
                "metal_memory_bytes",
            )
        ):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value):
            # Histogram bucket bounds and outcome labels cannot be added together.
            key = name + (labels or "")
            values[key] = value
    return values


def gpu_utilization(value: Any) -> float | None:
    if isinstance(value, dict):
        statistics = value.get("PerformanceStatistics")
        if isinstance(statistics, dict) and isinstance(statistics.get("Device Utilization %"), (float, int)):
            return float(statistics["Device Utilization %"])
        for child in value.values():
            found = gpu_utilization(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = gpu_utilization(child)
            if found is not None:
                return found
    return None


def resource_sample(model_url: str) -> dict[str, Any]:
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    sample: dict[str, Any] = {
        "at": time.time(),
        "cpu_percent": psutil.cpu_percent(),
        "memory_total_bytes": memory.total,
        "memory_available_bytes": memory.available,
        "memory_used_bytes": memory.used,
        "swap_used_bytes": swap.used,
        "swap_in_bytes": swap.sin,
        "swap_out_bytes": swap.sout,
    }
    try:
        raw = subprocess.check_output(["/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator", "-a"], timeout=5)
        sample["gpu_percent"] = gpu_utilization(plistlib.loads(raw))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sample["gpu_unavailable"] = type(error).__name__
    try:
        response = httpx.get(model_url.rstrip("/") + "/metrics", timeout=5, trust_env=False)
        response.raise_for_status()
        sample["engine"] = prometheus_values(response.text)
    except httpx.HTTPError as error:
        sample["engine_unavailable"] = type(error).__name__
    return sample


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize_resources(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"samples": len(samples)}
    for field in (
        "cpu_percent",
        "gpu_percent",
        "memory_used_bytes",
        "memory_available_bytes",
        "swap_used_bytes",
    ):
        values = [float(s[field]) for s in samples if isinstance(s.get(field), (int, float))]
        result[field] = {
            "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95),
            "max": max(values) if values else None,
        }
    for field in ("swap_used_bytes", "swap_in_bytes", "swap_out_bytes"):
        values = [float(s[field]) for s in samples if isinstance(s.get(field), (int, float))]
        result[field + "_delta"] = values[-1] - values[0] if values else None
    for metric, aliases in (
        ("num_requests_running", ("num_requests_running", "scheduler_running_requests")),
        ("num_requests_waiting", ("num_requests_waiting", "scheduler_waiting_requests")),
    ):
        values = [
            s["engine"]["sentrix_mlx_num_requests_waiting"]
            if metric == "num_requests_waiting" and "sentrix_mlx_num_requests_waiting" in s["engine"]
            else sum(v for k, v in s["engine"].items() if k.split("{", 1)[0].endswith(aliases))
            for s in samples
            if isinstance(s.get("engine"), dict)
            and any(k.split("{", 1)[0].endswith(aliases) for k in s["engine"])
        ]
        result["engine_" + metric + "_peak"] = max(values) if values else None
    result["engine_metrics_missing_samples"] = sum(not s.get("engine") for s in samples)
    return result


def observe_run(
    bench_url: str, model_url: str, output: Path, run_id: str, *, deadline_s: float = 86400
) -> dict[str, Any]:
    """Resume observation only; never resume/replay QA or silently replace results."""
    started = time.monotonic()
    journal = output / "resources.jsonl"
    samples = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    with httpx.Client(base_url=bench_url.rstrip("/"), timeout=30, trust_env=False) as client:
        while True:
            sample = resource_sample(model_url)
            samples.append(sample)
            with journal.open("a") as stream:
                stream.write(json.dumps(sample) + "\n")
            response = client.get(f"/api/v1/runs/{run_id}", params={"include_history": "false"})
            # POST /runs returns 202 before its worker publishes the first state.
            # Retry only this read, never the already accepted run submission.
            if response.status_code == 404 and time.monotonic() - started < 60:
                time.sleep(1)
                continue
            response.raise_for_status()
            state: dict[str, Any] = response.json()
            if not isinstance(state, dict):
                raise ValueError("run state must be an object")
            save(output / "latest-run.private.json", state)
            save(
                output / "observation.json",
                {"run_id": run_id, "status": state.get("status"), "observed_at": time.time()},
            )
            if state.get("status") in TERMINAL:
                save(output / "resource-summary.json", summarize_resources(samples))
                return state
            if time.monotonic() - started > deadline_s:
                raise TimeoutError("observation deadline; run outcome must be inspected before any new POST")
            time.sleep(5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    subset = commands.add_parser("freeze-subset")
    subset.add_argument("--source", type=Path, required=True)
    subset.add_argument("--destination", type=Path, required=True)
    observer = commands.add_parser("observe")
    observer.add_argument("--bench-url", required=True)
    observer.add_argument("--model-url", required=True)
    observer.add_argument("--output", type=Path, required=True)
    observer.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze-subset":
        print(json.dumps(freeze_subset(args.source, args.destination)))
    else:
        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        observe_run(args.bench_url, args.model_url, args.output, args.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
