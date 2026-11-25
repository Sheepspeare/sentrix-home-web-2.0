"""Watch an existing baseline through a priority subset without another POST."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from watch_baseline import BASE, TERMINAL, api, request_alert, save, ssh


def priority_ready(port, run_id, targets):
    completed = set()
    for page in range(1, 12):
        items = api(port, f"runs/{run_id}/items?page={page}&page_size=100")
        completed.update(item["qa_id"] for item in items.get("items", []))
        if not items.get("has_next"):
            return targets <= completed
    return False


def watch(group, run_id, audit, output, *, instance="baseline"):
    output.mkdir()
    port = {
        "baseline": {"limited": 8876, "large": 8877},
        "candidate": {"limited": 8878, "large": 8879},
    }[instance][group]
    targets = set(json.loads((audit / f"{group}-run-payload.json").read_text())["qa_priority_ids"])
    started = time.time()
    save(
        output / "intent.json",
        {
            "group": group,
            "run_id": run_id,
            "at": started,
            "priority_ids": sorted(targets),
            "mutates_run": False,
            "instance": instance,
            "evaluation_port": port,
        },
    )
    failures = 0
    try:
        while time.time() - started < 24 * 3600:
            try:
                snapshot = api(port, f"runs/{run_id}?include_history=false")
                save(output / "latest.json", snapshot)
                log_tail = ssh(f"tail -n 500 {BASE}/model-requests.jsonl")
                records = [json.loads(line) for line in log_tail.splitlines()]
                save(output / "request-metadata.json", records)
                disk = ssh("df -Pk /home").splitlines()[-1].split()
                free_bytes = int(disk[3]) * 1024
                failures = 0
            except (subprocess.SubprocessError, ValueError):
                failures += 1
                if failures >= 4:
                    raise
                time.sleep(20)
                continue
            progress = (snapshot.get("phases", {}).get("qa_eval") or {}).get("progress") or {}
            reason = request_alert(records, started)
            if free_bytes < 2 * 1024**3:
                reason = "disk_below_2GiB"
            if snapshot.get("status") in TERMINAL:
                reason = "run_terminal"
            if (
                not reason
                and progress.get("agent_completed", 0) >= len(targets)
                and priority_ready(port, run_id, targets)
            ):
                reason = "priority_subset_ready"
            if reason:
                result = {
                    "status": "needs_review",
                    "reason": reason,
                    "group": group,
                    "run_id": run_id,
                    "at": time.time(),
                    "free_bytes": free_bytes,
                    "progress": progress,
                    "worker_stopped_by_watcher": False,
                }
                save(output / "terminal.json", result)
                print(json.dumps(result), flush=True)
                return 0 if reason == "priority_subset_ready" else 1
            time.sleep(20)
        raise TimeoutError("Progress watcher deadline; inspect live work")
    except Exception as error:
        save(output / "terminal.json", {"status": "failed", "error": str(error), "at": time.time()})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=["limited", "large"], required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--instance", choices=["baseline", "candidate"], default="baseline")
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(watch(args.group, args.run_id, args.audit, args.output, instance=args.instance))
