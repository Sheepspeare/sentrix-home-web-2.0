"""Observe an existing run after reporting the 2 GiB warning; never alter workers."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from watch_baseline import BASE, TERMINAL, api, request_alert, save, ssh


def watch(port: int, run_id: str, output: Path) -> int:
    output.mkdir()
    started = time.time()
    save(
        output / "intent.json",
        {
            "run_id": run_id,
            "evaluation_port": port,
            "at": started,
            "mutates_run": False,
            "known_warning": "disk_below_2GiB_already_reported",
            "new_disk_alert_bytes": 1024**3,
            "terminal_only": True,
        },
    )
    try:
        while time.time() - started < 24 * 3600:
            snapshot = api(port, f"runs/{run_id}?include_history=false")
            work = api(port, "active-work")
            free = int(ssh("df -Pk /home").splitlines()[-1].split()[3]) * 1024
            records = [
                json.loads(line) for line in ssh(f"tail -n 500 {BASE}/model-requests.jsonl").splitlines()
            ]
            save(output / "latest.json", snapshot)
            save(output / "active-work.json", work)
            save(output / "request-metadata.json", records)
            save(
                output / "disk.json",
                {
                    "at": time.time(),
                    "free_bytes": free,
                    "warning_below_2GiB": free < 2 * 1024**3,
                    "warning_previously_reported": True,
                },
            )
            progress = snapshot.get("phases", {}).get("qa_eval", {}).get("progress", {})
            worker_alive = any(row.get("id") == run_id and row.get("worker_alive") for row in work["work"])
            reason = request_alert(records, started)
            if snapshot.get("status") in TERMINAL:
                reason = "run_terminal"
            elif not worker_alive:
                reason = "worker_not_alive"
            elif free < 1024**3:
                reason = "disk_deteriorated_below_1GiB"
            if reason:
                result = {
                    "at": time.time(),
                    "status": "needs_review",
                    "reason": reason,
                    "run_id": run_id,
                    "run_status": snapshot.get("status"),
                    "progress": progress,
                    "free_bytes": free,
                    "worker_alive": worker_alive,
                    "worker_stopped_by_watcher": False,
                }
                save(output / "terminal.json", result)
                print(json.dumps(result), flush=True)
                return 0 if reason == "run_terminal" else 1
            time.sleep(30)
        raise TimeoutError("Read-only terminal monitor reached 24 hours")
    except Exception as error:
        save(output / "terminal.json", {"status": "failed", "at": time.time(), "error": str(error)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(watch(args.port, args.run_id, args.output))
