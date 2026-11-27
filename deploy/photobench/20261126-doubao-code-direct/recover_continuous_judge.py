"""Recover missing scores while the original QA run continues unchanged."""

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from representative_qa import load, process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
ORIGINAL = B / "planner-large-continue16-20261127"
ROOT = B / "planner-large-judge-recovery1-20261127"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"
SOURCE = B / "flash-dual-baseline-20261126/release/apps/photobench/src"


def prepare():
    assert not ROOT.exists()
    folder = ORIGINAL / "prepared/planner-large"
    g = json.loads((folder / "started.json").read_text())
    assert Path(f"/proc/{g['pid']}").exists()
    old_pid = json.loads((folder / "judge-process.json").read_text())["pid"]
    proc = Path(f"/proc/{old_pid}/stat")
    assert not proc.exists() or proc.read_text().split(") ")[1].split()[0] == "Z"
    g["previous_score_paths"] = [str(folder / "judge/preserved-merged.json")]
    previous = json.loads((folder / "judge/preserved-scores.json").read_text())
    valid = {q: v["result"] for q, v in previous.items()}
    diagnostics = {}
    for p in sorted((folder / "judge").glob("batch-*/scoring-state.json")):
        s = json.loads(p.read_text())
        for v in s.get("scoring_results", {}).values():
            d = s.get("diagnostic_results", {}).get(v.get("diagnostic_key"), {})
            if v.get("status") == "completed" and d.get("status") == "completed":
                valid[v["qa_id"]] = v
                diagnostics[v["diagnostic_key"]] = d
    # Retain original diagnostics for inherited valid results.
    for v in previous.values():
        s = json.loads(Path(v["path"]).read_text())
        r = v["result"]
        diagnostics[r["diagnostic_key"]] = s["diagnostic_results"][r["diagnostic_key"]]
    ROOT.mkdir()
    target = ROOT / "planner-large"
    target.mkdir()
    merged = ROOT / "preserved-merged.json"
    save(merged, {"scoring_results": valid, "diagnostic_results": diagnostics})
    g["previous_score_paths"] = [str(merged)]
    save(
        ROOT / "intent.json",
        {
            "groups": [g],
            "source_root": str(ORIGINAL),
            "reused_valid": len(valid),
            "qa_unchanged": True,
            "at": time.time(),
        },
    )
    save(target / "started.json", g)
    (target / "results").symlink_to(folder / "results", target_is_directory=True)
    print(json.dumps({"reused_valid": len(valid), "qa_pid": g["pid"]}))


def worker():
    plan = json.loads((ROOT / "intent.json").read_text())
    g = plan["groups"][0]
    module = load("recovered_continue", ORIGINAL / "continue_four.py")
    try:
        module.judge(ROOT, g, threading.Event())
    except Exception as exc:
        save(ROOT / "failure.json", {"error": str(exc), "type": type(exc).__name__})
        raise


def launch():
    assert not (ROOT / "launch.json").exists()
    _, _, env = process_environment(8877)
    env["PYTHONPATH"] = str(SOURCE) + ":" + env.get("PYTHONPATH", "")
    log = (ROOT / "judge.log").open("x")
    child = subprocess.Popen(
        [sys.executable, __file__, "--worker"], env=env, stdout=log, stderr=log, start_new_session=True
    )
    save(ROOT / "launch.json", {"pid": child.pid, "concurrency": 2, "at": time.time()})
    print(json.dumps({"judge_pid": child.pid, "concurrency": 2}))


def status():
    from photobench.infrastructure.results.store import FileRunStore

    g = json.loads((ROOT / "intent.json").read_text())["groups"][0]
    s = FileRunStore(ROOT / "planner-large/results").load_state(g["run_id"])
    result = {
        "status": "running",
        "qa_status": s["status"],
        "processed": g["inherited"] + len(s["items"]),
        "qa_pid": g["pid"],
        "qa_alive": Path(f"/proc/{g['pid']}").exists(),
        "at": time.time(),
    }
    for name in ["progress", "terminal"]:
        p = ROOT / "planner-large/judge" / (name + ".json")
        if p.exists():
            result["judge_" + name] = json.loads(p.read_text())
    if (ROOT / "failure.json").exists():
        result["failure"] = json.loads((ROOT / "failure.json").read_text())
    pid = json.loads((ROOT / "launch.json").read_text())["pid"]
    stat = Path(f"/proc/{pid}/stat")
    result["judge_alive"] = stat.exists() and stat.read_text().split(") ")[1].split()[0] != "Z"
    if result.get("judge_terminal"):
        result["status"] = result["judge_terminal"]["status"]
    elif result.get("failure") or not result["judge_alive"] or s.get("fatal_error"):
        result["status"] = "needs_review"
    return result


def watch(output):
    output.mkdir(parents=True, exist_ok=True)
    while True:
        call = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "192.168.0.100",
                f"PYTHONPATH={SOURCE} {PYTHON} {ROOT}/recover_continuous_judge.py --status",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if call.returncode:
            save(output / "observer-error.json", {"stderr": call.stderr[-1500:]})
            return 1
        data = json.loads(call.stdout)
        save(output / "latest.json", data)
        if data["status"] != "running":
            save(output / "terminal.json", data)
            return 0 if data["status"] == "completed" else 1
        time.sleep(30)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--launch", action="store_true")
    p.add_argument("--worker", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--watch", type=Path)
    a = p.parse_args()
    if a.prepare:
        prepare()
    elif a.launch:
        launch()
    elif a.worker:
        worker()
    elif a.status:
        print(json.dumps(status()))
    else:
        raise SystemExit(watch(a.watch))
