"""Wake once the exact snapshot archive finishes or its worker disappears."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "20261118-atm1013-full"))
from monitor_event import emit_event

parser = argparse.ArgumentParser(__doc__)
parser.add_argument("--registration", type=Path, required=True)
parser.add_argument("--audit", type=Path, required=True)
parser.add_argument("--remote-directory", required=True)
args = parser.parse_args()
failures = 0
started = time.monotonic()
while True:
    reason = ""
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
                "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python",
                args.remote_directory + "/archive_snapshots.py",
                "probe",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        snapshot = json.loads(result.stdout)
        if snapshot.get("status") in {"failed", "completed"} or not snapshot["worker_alive"]:
            reason = "Inactive legacy snapshot compression ended; verify receipts and reattach Doubao monitor"
        if not reason:
            command = [
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
                "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python",
                "/home/realmagic/xy/experiments/atm1013-20261118/doubao-start-v1/probe_run.py",
            ]
            probe = subprocess.run(command, capture_output=True, text=True, timeout=90, check=True)
            run = json.loads(probe.stdout)
            (args.audit / "doubao-during-archive-latest.json").write_text(json.dumps(run, indent=2))
            assert run["run_id"] == "20261120-020624-2026.11.17-atm-sources-addresses-current-d52ecf"
            if run["status"] != "running" or not any(
                row["id"] == run["run_id"] and row["worker_alive"] for row in run["active"]
            ):
                reason = "Original Doubao run ended or lost its worker during storage recovery"
            elif (
                not all(
                    run[key]
                    for key in (
                        "model_matches",
                        "service_pids_match",
                        "product_limits_match",
                        "product_database_match",
                    )
                )
                or run["reasoning_context_tokens"] != 16384
            ):
                reason = "Original Doubao runtime changed during storage recovery"
            elif (
                run["memory_available_kib"] < 4 * 1024**2
                or max(run["process_rss_kib"].values()) > 24 * 1024**2
            ):
                reason = "Doubao memory exceeded the existing resource limits"
            elif min(run["disk_free"].values()) < 512 * 1024**2:
                reason = "Known low disk fell below 512 MiB during the exact compression recovery"
        failures = 0
        if not reason and time.monotonic() - started > 2 * 3600:
            reason = "Exact snapshot compression exceeded two hours"
    except Exception as error:
        failures += 1
        snapshot = {"consecutive_failures": failures, "error": str(error)[:1000]}
        if failures >= 3:
            reason = "Three snapshot archive probes failed"
    evidence = args.audit / "archive-latest.json"
    evidence.write_text(json.dumps(snapshot, indent=2))
    if reason:
        event = args.audit / "archive-event.json"
        event.write_text(
            json.dumps(
                {"status": "needs_review", "reason": reason, "snapshot": snapshot, "evidence": str(evidence)},
                indent=2,
            )
        )
        emit_event(args.registration, event)
        break
    time.sleep(30)
