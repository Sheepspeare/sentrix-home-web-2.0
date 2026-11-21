"""Persist isolated checkpoint profiling evidence before waking the same chat."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "20261118-atm1013-full"))
from monitor_event import emit_event

SSH = [
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
]
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--remote-directory", required=True)
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures = 0
    started = time.monotonic()
    while True:
        reason = ""
        try:
            command = SSH + [
                PYTHON,
                args.remote_directory + "/inspect_profile.py",
                "probe",
                "--worker-pid",
                str(args.worker_pid),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=90)
            result = json.loads(completed.stdout)
            failures = 0
            if result["accepted"] or not result["worker_alive"]:
                reason = "Isolated Doubao checkpoint profiling ended; inspect timings and retained answers"
            elif time.monotonic() - started > 3600:
                reason = "Isolated Doubao checkpoint profiling exceeded one hour"
        except Exception as error:
            failures += 1
            result = {"worker_pid": args.worker_pid, "consecutive_failures": failures, "error": str(error)}
            if isinstance(error, subprocess.CalledProcessError):
                result["stderr"] = (error.stderr or "")[-2000:]
            if failures >= 3:
                reason = "Three consecutive Doubao checkpoint profiling probes failed"
        evidence = args.audit / "profile-latest.json"
        evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if args.check:
            print(json.dumps({"snapshot": result, "reason": reason}), flush=True)
            return
        if reason:
            copies = []
            if not result.get("worker_alive", True):
                for name in (
                    "profile-acceptance.json",
                    "profile-error.json",
                    "profile.log",
                    "write-0-profile.txt",
                    "write-1-profile.txt",
                ):
                    copied = subprocess.run(
                        [
                            "scp",
                            "-q",
                            "-o",
                            "BatchMode=yes",
                            "-o",
                            "ConnectTimeout=10",
                            "192.168.0.100:" + args.remote_directory + "/" + name,
                            str(args.audit / name),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    copies.append({"file": name, "copied": copied.returncode == 0})
            event = args.audit / ("profile-event-" + str(int(time.time())) + ".json")
            event.write_text(
                json.dumps(
                    {
                        "status": "needs_review",
                        "reason": reason,
                        "snapshot": result,
                        "copies": copies,
                        "evidence": str(evidence),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            emit_event(args.registration, event)
            return
        time.sleep(45)


if __name__ == "__main__":
    main()
