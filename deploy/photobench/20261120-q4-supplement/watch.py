"""Monitor one Q4 supplement without model polling; emit compact durable events."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "20261118-atm1013-full"))
from monitor_event import emit_event, terminal_event

REMOTE = "/home/realmagic/xy/experiments/atm1013-20261118/q4-supplement-v1/supplement.py"
APP = "/home/realmagic/xy/experiments/atm1013-20261118/chat-trace-8872-7b34a75b/apps/photobench/src"


def reason(snapshot):
    if snapshot["status"] not in {"pending", "running", "cancelling"}:
        return "Q4 missing-answer supplement reached terminal state; verify attempts, scores and exports"
    if not any(w["id"] == snapshot["run_id"] and w["worker_alive"] for w in snapshot["active"]):
        return "Exact Q4 supplement worker disappeared"
    if not all(
        snapshot[k]
        for k in (
            "model_matches",
            "mapping_matches",
            "budget_matches",
            "services_match",
            "limits_match",
            "source_manifest_unchanged",
            "reasoning",
            "graph_enabled",
        )
    ):
        return "Q4 supplement model, process, budget, source manifest or active mapping changed"
    if min(snapshot["disk_free"].values()) < 1536 * 1024**2:
        return "Disk free space below 1.5 GiB"
    if snapshot["memory_available_kib"] < 4 * 1024**2:
        return "Available system memory below 4 GiB"
    if max(s["rss_kib"] for s in snapshot["services"].values()) > 24 * 1024**2:
        return "Q4 service RSS exceeded 24 GiB"
    return ""


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures = 0
    started = time.monotonic()
    while True:
        review = ""
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "ServerAliveInterval=15",
                    "-o",
                    "ServerAliveCountMax=2",
                    "192.168.0.100",
                    "env",
                    "PYTHONPATH=" + APP,
                    "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python",
                    REMOTE,
                    "probe",
                    "--run-id",
                    args.run_id,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            snapshot = json.loads(result.stdout)
            assert snapshot["run_id"] == args.run_id
            failures = 0
            review = reason(snapshot)
            if not review and time.monotonic() - started > 24 * 3600:
                review = "Q4 34-question supplement exceeded 24-hour review window"
        except Exception as error:
            failures += 1
            snapshot = {
                "stage": "q4km",
                "run_id": args.run_id,
                "consecutive_failures": failures,
                "probe_error": str(error)[:2000],
            }
            if isinstance(error, subprocess.CalledProcessError):
                snapshot["probe_stderr"] = (error.stderr or "")[-3000:]
            if failures >= 3:
                review = "Three consecutive exact Q4 supplement probes failed"
        evidence = args.audit / "q4km-latest.json"
        evidence.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        if args.check:
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": snapshot.get("status"),
                        "reason": review,
                        "failures": failures,
                    }
                ),
                flush=True,
            )
            return int(bool(review or failures))
        if review:
            event = args.audit / ("event-" + str(int(time.time())) + ".json")
            event.write_text(
                json.dumps(terminal_event(review, snapshot, evidence), ensure_ascii=False, indent=2)
            )
            emit_event(args.registration, event)
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
