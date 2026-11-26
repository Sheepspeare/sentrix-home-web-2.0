"""Read-only watch of the two new runs; never launches or retries QA."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path

BASE = "/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-doubao-code-direct-20261126"
PORTS = {"limited": 8881, "large": 8882}
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "192.168.0.100"]
TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}


def ssh(command):
    return subprocess.check_output([*SSH, command], text=True, timeout=90)


def api(port, path):
    return json.loads(ssh("curl --max-time 60 -fsS " + shlex.quote(f"http://127.0.0.1:{port}/api/v1/{path}")))


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def watch(audit):
    launch = json.loads((audit / "launch.json").read_text())
    assert set(launch) == set(PORTS)
    output = audit / "checkpoint"
    output.mkdir()
    save(
        output / "intent.json",
        {"at": time.time(), "launch": launch, "mode": "read_only_attach", "target": 16},
    )
    done = {}
    errors = []
    deadline = time.time() + 3 * 3600
    while len(done) < 2:
        for group, port in PORTS.items():
            if group in done:
                continue
            run = launch[group]["run_ids"][0]
            try:
                snapshot = api(port, f"runs/{run}?include_history=false")
                save(output / f"{group}-latest.json", snapshot)
                progress = snapshot.get("phases", {}).get("qa_eval", {}).get("progress", {})
                records = [
                    json.loads(line) for line in ssh(f"cat {BASE}/{group}/http/requests.jsonl").splitlines()
                ]
                save(output / f"{group}-requests.json", records)
                if snapshot.get("status") in TERMINAL or (
                    progress.get("dispatch_paused") and progress.get("agent_completed", 0) == 16
                ):
                    save(output / f"{group}-items.json", api(port, f"runs/{run}/items?page=1&page_size=100"))
                    done[group] = {
                        "run_id": run,
                        "status": snapshot.get("status"),
                        "progress": progress,
                        "fatal_error": snapshot.get("fatal_error"),
                        "at": time.time(),
                    }
                    save(output / f"{group}-terminal.json", done[group])
            except Exception as error:
                errors.append({"at": time.time(), "group": group, "error": str(error)})
                save(output / "read-errors.json", errors)
                if len(errors) >= 6:
                    raise
        if time.time() > deadline:
            raise TimeoutError("Inspect the existing run and in-flight turns; no automatic replay")
        if len(done) < 2:
            time.sleep(30)
    save(
        output / "terminal.json",
        {"status": "needs_review", "groups": done, "at": time.time(), "qa_replayed": False},
    )
    print(json.dumps(done), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    args = parser.parse_args()
    try:
        watch(args.audit)
    except BaseException as error:
        save(args.audit / "watch-terminal.json", {"status": "failed", "error": str(error), "at": time.time()})
        raise
