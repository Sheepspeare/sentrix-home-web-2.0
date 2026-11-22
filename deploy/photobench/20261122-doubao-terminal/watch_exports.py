"""Watch the exact Doubao export validator and wake this thread on completion or error."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "20261118-atm1013-full"))
from monitor_event import emit_event

parser = argparse.ArgumentParser(__doc__)
parser.add_argument("--worker-pid", type=int, required=True)
parser.add_argument("--registration", type=Path, required=True)
parser.add_argument("--audit", type=Path, required=True)
parser.add_argument("--remote-directory", required=True)
parser.add_argument("--app-dir", required=True)
parser.add_argument("--check", action="store_true")
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
                "env",
                "PYTHONPATH=" + args.app_dir,
                "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python",
                args.remote_directory + "/verify_exports.py",
                "probe",
                "--worker-pid",
                str(args.worker_pid),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        snapshot = json.loads(result.stdout)
        failures = 0
        if snapshot["accepted"] and not snapshot["worker_alive"]:
            reason = "Doubao complete trace and SFT exports reached acceptance; inspect both receipts"
        elif not snapshot["worker_alive"]:
            reason = "Exact Doubao export validator stopped before acceptance"
        elif not all(
            snapshot[k]
            for k in (
                "evaluation_pid_matches",
                "evaluation_app_matches",
                "exports_source_matches",
                "revision_matches",
            )
        ):
            reason = "Pinned Doubao export revision or evaluator process changed"
        elif snapshot["memory_available_kib"] < 4 * 1024**2:
            reason = "Available system memory below 4 GiB during Doubao exports"
        elif (
            snapshot.get("phase") != "waiting_storage"
            and min(snapshot["disk_free"].values()) < 1536 * 1024**2
        ):
            reason = "Disk free space below 1.5 GiB during Doubao exports"
        elif time.monotonic() - started > 4 * 3600:
            reason = "Doubao export validation exceeded four hours"
    except Exception as error:
        failures += 1
        snapshot = {
            "worker_pid": args.worker_pid,
            "consecutive_failures": failures,
            "error": str(error)[:2000],
        }
        if failures >= 3:
            reason = "Three consecutive exact Doubao export probes failed"
    evidence = args.audit / "exports-latest.json"
    evidence.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if args.check:
        print(json.dumps({"snapshot": snapshot, "reason": reason}))
        sys.exit(1 if failures else 0)
    if reason:
        event = args.audit / ("exports-event-" + str(int(time.time())) + ".json")
        event.write_text(
            json.dumps(
                {"status": "needs_review", "reason": reason, "snapshot": snapshot, "evidence": str(evidence)},
                ensure_ascii=False,
                indent=2,
            )
        )
        emit_event(args.registration, event)
        break
    time.sleep(60)
