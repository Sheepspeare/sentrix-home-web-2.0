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
args = parser.parse_args()
failures = 0
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
                "/home/realmagic/xy/experiments/atm1013-20261118/doubao-disk-recovery-v1/archive_snapshots.py",
                "probe",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        snapshot = json.loads(result.stdout)
        failures = 0
        if snapshot.get("status") in {"failed", "completed"} or not snapshot["worker_alive"]:
            reason = "Inactive legacy snapshot compression ended; verify receipts and reattach Doubao monitor"
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
