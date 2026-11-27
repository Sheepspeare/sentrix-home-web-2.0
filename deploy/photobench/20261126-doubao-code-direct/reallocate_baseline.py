"""Pause the old limited dispatcher and continue the original baseline at sixteen."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from representative_qa import get, process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
ORIGINAL = B / "flash-dual-baseline-20261126"
FOUR = B / "full-four-continue-20261127"
RUN = "20261126-025319-2026.11.17-atm-sources-addresses-big_model-f6ebb1"


def active(port):
    _, _, env = process_environment(port)
    with sqlite3.connect(
        (Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db").as_uri() + "?mode=ro", uri=True
    ) as db:
        return db.execute(
            "select count(*) from agent_turns where status in ('pending','running')"
        ).fetchone()[0]


def apply(root, resume_paused=False):
    if not resume_paused:
        assert not (root / "intent.json").exists(), "Inspect existing receipt before retry"
    else:
        assert (root / "intent.json").exists()
        assert not (root / "original-control-before.json").exists()
        assert not (root / "launched.json").exists()
    root.mkdir(exist_ok=True)
    old = json.loads((FOUR / "prepared/baseline-limited/started.json").read_text())
    pid = old["pid"]
    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
    assert "continue_four.py" in cmd and "--worker\x00baseline-limited" in cmd
    save(
        root / "intent.json",
        {
            "pause_pid": pid,
            "pause_run": old["run_id"],
            "continue_run": RUN,
            "qa_concurrency": 16,
            "judge_concurrency": 2,
            "at": time.time(),
        },
    )
    # Suspend only the independent evaluator. Product turns finish on the product service.
    if not resume_paused:
        os.kill(pid, signal.SIGSTOP)
    for _ in range(20):
        if "\nState:\tT" in Path(f"/proc/{pid}/status").read_text():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Dispatcher not stopped")
    save(
        FOUR / "prepared/baseline-limited/intentional-pause.json",
        {
            "status": "paused",
            "pid": pid,
            "reason": "user_reallocate_to_original_planner_large",
            "product_turns_preserved": True,
            "at": time.time(),
        },
    )
    control = ORIGINAL / "large/dispatch-control.json"
    save(root / "original-control-before.json", json.loads(control.read_text()))
    save(control, {"concurrency": 4, "pause_after_completed": 1})
    stable = 0
    for _ in range(500):
        live = get(8877, f"runs/{RUN}?include_history=false")
        phase = live["phases"]["qa_eval"]
        n = active(8297)
        good = n == 0 and len(phase["agent_executed_qa_ids"]) == phase["progress"]["agent_completed"]
        save(
            root / "progress.json",
            {
                "status": "draining_original",
                "active": n,
                "processed": phase["progress"]["agent_completed"],
                "old_limited_suspended": True,
                "old_limited_product_active": active(8305),
                "at": time.time(),
            },
        )
        stable = stable + 1 if good else 0
        if stable == 2:
            break
        time.sleep(15)
    assert stable == 2, "Original admission remains paused; inspect drain"
    from photobench.infrastructure.results.store import FileRunStore

    _, words, env = process_environment(8877)
    state = FileRunStore(Path(env["PHOTOBENCH_RESULTS_ROOT"])).load_state(RUN)
    assert state["build_revision"].startswith("30c67e74")
    present = {v["qa_id"] for v in state["items"]}
    rows = state["qa_snapshot"]["rows"]
    assert len(rows) == 1013 and len(present) == len(state["items"])
    # Only this idle product is restarted, with the same source, DB and model settings.
    unit = "sentrix-flash-dual-large-api.service"
    conf = get(8297, "diagnostics")["runtime_configuration"]
    assert conf["vlm_context_tokens"] == conf["vlm_reasoning_context_tokens"] == 131072
    assert conf["reasoning_max_model_calls"] == 100 and conf["reasoning_max_tool_calls"] == 600
    drop = Path.home() / ".config/systemd/user" / (unit + ".d/90-authorized-16-workers.conf")
    drop.parent.mkdir(exist_ok=True)
    drop.write_text('[Service]\nEnvironment="SENTRIX_AGENT_TURN_MAX_WORKERS=16"\n')
    runtime = ORIGINAL / "large/config/runtime_connection.json"
    connection = json.loads(runtime.read_text())
    save(root / "runtime-before.json", connection)
    connection["product_settings"]["agent_turn_workers"] = 16
    save(runtime, connection)
    api_env = ORIGINAL / "large-api.env"
    text = api_env.read_text()
    old_workers = 'SENTRIX_AGENT_TURN_MAX_WORKERS="4"'
    assert text.count(old_workers) == 1
    (root / "api-env-before.txt").write_text(text)
    (root / "api-env-before.txt").chmod(0o600)
    api_env.write_text(text.replace(old_workers, 'SENTRIX_AGENT_TURN_MAX_WORKERS="16"'))
    api_env.chmod(0o600)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", unit], check=True, timeout=180)
    for _ in range(90):
        try:
            after = get(8297, "diagnostics")["runtime_configuration"]
            if after["agent_turn_workers"] == 16:
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        raise RuntimeError("Product workers not verified")
    assert {k: v for k, v in conf.items() if k != "agent_turn_workers"} == {
        k: v for k, v in after.items() if k != "agent_turn_workers"
    }
    prepared = root / "prepared"
    prepared.mkdir()
    name = "planner-large"
    folder = prepared / name
    folder.mkdir()
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint

    snapshot = dict(state["qa_snapshot"])
    snapshot["rows"] = [v for v in rows if v["qa_id"] not in present]
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    save(folder / "snapshot.json", snapshot)
    source = {"results": env["PHOTOBENCH_RESULTS_ROOT"], "run": RUN, "count": len(present)}
    save(
        folder / "inherited-manifest.json",
        {"qa_ids": sorted(present), "sources": [source], "frozen_total": 1013},
    )
    g = {
        "name": name,
        "port": 8877,
        "product": 8297,
        "source_run": RUN,
        "evaluation_source": words[words.index("--app-dir") + 1],
        "concurrency": 16,
        "inherited": len(present),
        "remaining": len(snapshot["rows"]),
        "sources": [source],
        "previous_score_paths": [str(root / "preserved-original-scores.json")],
    }
    save(prepared / "intent.json", {"groups": [g], "originals_remain_paused": True, "at": time.time()})

    env["PYTHONPATH"] = g["evaluation_source"] + ":" + env.get("PYTHONPATH", "")
    log = (root / "worker.log").open("x")
    proc = subprocess.Popen(
        [sys.executable, str(root / "continue_four.py"), str(prepared), "--worker", name],
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    save(root / "launched.json", {"pid": proc.pid, **g, "at": time.time()})
    print(
        json.dumps(
            {"pid": proc.pid, "inherited": len(present), "remaining": g["remaining"], "qa_concurrency": 16}
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--resume-paused", action="store_true")
    a = p.parse_args()
    try:
        apply(a.root, a.resume_paused)
    except Exception as e:
        save(
            a.root / "failure.json",
            {"status": "failed", "type": type(e).__name__, "error": str(e), "at": time.time()},
        )
        raise
