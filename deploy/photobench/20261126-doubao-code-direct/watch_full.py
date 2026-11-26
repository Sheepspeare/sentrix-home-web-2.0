"""Observe the two authorized full runs without restarting or submitting QA."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "192.168.0.100"]
TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def watch(root):
    plan = json.loads((root / "intent.json").read_text())
    assert not (root / "observer-started.json").exists(), "Attach existing observer; do not duplicate"
    save(root / "observer-started.json", {"at": time.time(), "read_only": True})
    failures = []
    while True:
        groups = {}
        for group in plan["groups"]:
            name, port, run = group["name"], group["port"], group["run_id"]
            try:
                url = f"http://127.0.0.1:{port}/api/v1/runs/{run}?include_history=false"
                raw = subprocess.check_output(
                    [*SSH, "curl --max-time 40 -fsS '" + url + "'"], text=True, timeout=50
                )
                live = json.loads(raw)
                assert live["run_id"] == run and live["build_revision"] == plan["agent_revision"]
                assert live["scope_id"] == plan["scope_id"]
                progress = live["phases"]["qa_eval"]["progress"]
                assert progress["qa_concurrency"] == 16 and progress["agent_total"] == 1013
                save(root / (name + "-latest.json"), live)
                groups[name] = {
                    "run_id": run,
                    "status": live["status"],
                    "answered": progress["agent_completed"],
                    "total": 1013,
                    "concurrency": progress["qa_concurrency"],
                    "dispatch_paused": progress["dispatch_paused"],
                    "fatal_error": live.get("fatal_error"),
                }
                if live.get("fatal_error") or (
                    live["status"] in TERMINAL and progress["agent_completed"] < 1013
                ):
                    raise RuntimeError((name, groups[name]))
            except Exception as error:
                failures.append({"group": name, "at": time.time(), "error": str(error)})
                save(root / "observer-read-errors.json", failures)
                if len(failures) >= 6 or isinstance(error, (AssertionError, RuntimeError)):
                    raise
        save(root / "progress.json", {"at": time.time(), "groups": groups})
        if len(groups) == 2 and all(g["answered"] == 1013 for g in groups.values()):
            save(
                root / "observer-terminal.json", {"status": "completed", "at": time.time(), "groups": groups}
            )
            return
        if time.time() >= plan["report_at"]:
            save(
                root / "observer-terminal.json",
                {"status": "report_checkpoint", "at": time.time(), "groups": groups},
            )
            return
        time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    root = parser.parse_args().root
    try:
        watch(root)
    except BaseException as error:
        save(
            root / "observer-terminal.json",
            {"status": "failed", "at": time.time(), "error_type": type(error).__name__, "error": str(error)},
        )
        raise
