"""Observe server-only QA and Judge work; return compact status only."""

import argparse
import json
import subprocess
import time
from pathlib import Path

REMOTE = "/home/realmagic/xy/benchmarks/atm-bench/experiments/planner-large-continue16-20261127"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"


def status(root):
    from photobench.infrastructure.results.store import FileRunStore

    failure = root.parent / "failure.json"
    if failure.exists():
        return json.loads(failure.read_text())
    if not (root / "intent.json").exists():
        progress = root.parent / "progress.json"
        return {
            "status": "running",
            "transition": json.loads(progress.read_text()) if progress.exists() else "starting",
        }
    plan = json.loads((root / "intent.json").read_text())
    rows = []
    failed = False
    complete = True
    for g in plan["groups"]:
        folder = root / g["name"]
        row = {k: g[k] for k in ["name", "inherited", "remaining", "concurrency"]}
        for name in ["started", "qa-terminal", "terminal", "failure", "judge-failure", "judge-process"]:
            p = folder / (name + ".json")
            if p.exists():
                d = json.loads(p.read_text())
                if name == "started":
                    row["run_id"] = d["run_id"]
                    row["pid"] = d["pid"]
                    row["alive"] = Path(f"/proc/{d['pid']}").exists()
                else:
                    row[name] = d
        if row.get("run_id"):
            try:
                state = FileRunStore(folder / "results").load_state(row["run_id"])
                row["qa_status"] = state["status"]
                row["new_processed"] = len(state["items"])
                row["total_processed"] = g["inherited"] + len(state["items"])
                row["qa_progress"] = state.get("phases", {}).get("qa_eval", {}).get("progress", {})
                row["fatal_error"] = state.get("fatal_error")
            except KeyError:
                row["qa_status"] = "starting"
        for name in ["progress", "terminal"]:
            p = folder / "judge" / (name + ".json")
            if p.exists():
                row["judge_" + name] = json.loads(p.read_text())
        p = folder / "judge/preserved-scores.json"
        if p.exists():
            row["preserved_valid"] = len(json.loads(p.read_text()))
        if row.get("failure") or row.get("judge-failure") or row.get("fatal_error"):
            failed = True
        if row.get("judge_terminal", {}).get("status") not in [None, "completed"]:
            failed = True
        if row.get("terminal", {}).get("status") not in [None, "completed"]:
            failed = True
        if row.get("alive") is False and not row.get("terminal"):
            failed = True
        complete &= row.get("terminal", {}).get("status") == "completed"
        rows.append(row)
    return {
        "status": "needs_review" if failed else "completed" if complete else "running",
        "groups": rows,
        "at": time.time(),
    }


def watch(output):
    output.mkdir(parents=True, exist_ok=True)
    while True:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "192.168.0.100",
                (
                    f"PYTHONPATH={REMOTE}/../flash-doubao-code-flash-memory-direct-20261126/"
                    f"release/apps/photobench/src {PYTHON} {REMOTE}/watch_reallocation.py "
                    f"--status {REMOTE}/prepared"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode:
            (output / "observer-error.json").write_text(
                json.dumps({"returncode": result.returncode, "stderr": result.stderr[-1800:]})
            )
            return 1
        data = json.loads(result.stdout)
        temp = output / "latest.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temp.replace(output / "latest.json")
        if data["status"] != "running":
            (output / "terminal.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data["status"] == "completed" else 1
        time.sleep(30)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--status", type=Path)
    p.add_argument("--watch", type=Path)
    a = p.parse_args()
    if a.status:
        print(json.dumps(status(a.status), ensure_ascii=False))
    else:
        raise SystemExit(watch(a.watch))
