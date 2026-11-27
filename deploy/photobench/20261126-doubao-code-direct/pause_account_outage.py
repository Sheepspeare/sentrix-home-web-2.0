"""Pause evaluator dispatch on confirmed account outage; preserve product turns."""

import json
import os
import signal
import time
from pathlib import Path

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/full-disk-recovery-20261127")


def main():
    receipt = B / "account-outage-pause-20261128.json"
    assert not receipt.exists()
    root = B / "prepared"
    evidence = []
    for name in ["baseline-large", "improved-large"]:
        paths = sorted((root / name / "judge").glob("batch-*/scoring-state.json"))
        state = json.loads(paths[-1].read_text())
        errors = [
            r.get("reason", "") for r in state["scoring_results"].values() if r.get("status") == "failed"
        ]
        assert any("Arrearage" in e and "HTTP 400" in e for e in errors)
        evidence.append({"group": name, "state": str(paths[-1]), "reasons": errors})
    paused = []
    for name in ["baseline-large", "improved-large", "improved-limited", "planner-large"]:
        started = json.loads((root / name / "started.json").read_text())
        pid = started["pid"]
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        assert "--worker" in cmd and name in cmd and str(B) in cmd
        os.kill(pid, signal.SIGSTOP)
        paused.append({"name": name, "pid": pid, "run_id": started["run_id"]})
    receipt.write_text(
        json.dumps(
            {
                "at": time.time(),
                "evidence": evidence,
                "paused": paused,
                "product_processes_unchanged": True,
                "inflight_not_cancelled": True,
            },
            indent=2,
        )
    )
    print(json.dumps({"paused": paused, "receipt": str(receipt)}))


if __name__ == "__main__":
    main()
