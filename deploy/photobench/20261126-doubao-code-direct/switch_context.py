"""Drain the two authorized QA groups, change their context, and resume admission."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path(
    "/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-doubao-code-flash-memory-direct-20261126"
)
RAM = Path("/dev/shm/sentrix-doubao-code-flash-memory-direct-20261126")
GROUPS = {"limited": (8305, 8885, "801a7b", 20, 30), "large": (8306, 8886, "2db84c", 100, 600)}
CONTEXT = 131072
KEYS = ("SENTRIX_VLM_CONTEXT_TOKENS", "SENTRIX_VLM_REASONING_CONTEXT_TOKENS")


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=60) as response:
        return json.load(response)


def unit(name, role):
    return f"sentrix-flash-own-memory-direct-{name}-{role}.service"


def pid(name, role):
    return int(
        subprocess.check_output(
            ["systemctl", "--user", "show", unit(name, role), "-p", "MainPID", "--value"], text=True
        ).strip()
    )


def context_env(text):
    lines = text.splitlines()
    for key in KEYS:
        matches = [n for n, line in enumerate(lines) if line.startswith(key + "=")]
        assert len(matches) == 1
        n = matches[0]
        assert json.loads(lines[n].split("=", 1)[1]) == "16384"
        lines[n] = key + "=" + json.dumps(str(CONTEXT))
    return "\n".join(lines) + "\n"


def drained(live, active):
    phase = live["phases"]["qa_eval"]
    progress = phase["progress"]
    return (
        progress["dispatch_paused"]
        and active == 0
        and len(phase["agent_executed_qa_ids"]) == progress["agent_completed"]
    )


def switch(name, root):
    port, evaluation, suffix, models, tools = GROUPS[name]
    run = "20261126-225652-2026.11.17-atm-sources-addresses-current-" + suffix
    folder = root / name
    folder.mkdir()
    control = BASE / name / "dispatch-control.json"
    env = BASE / (name + "-api.env")
    runtime = BASE / name / "config/runtime_connection.json"
    live = get(evaluation, f"runs/{run}?include_history=false")
    assert live["run_id"] == run and live["status"] == "running"
    assert live["phases"]["qa_eval"]["progress"]["qa_concurrency"] == 16
    before = json.loads(control.read_text())
    assert before == {"concurrency": 16, "pause_after_completed": 1013}
    config = get(port, "diagnostics")["runtime_configuration"]
    assert config["vlm_context_tokens"] == config["vlm_reasoning_context_tokens"] == 16384
    assert config["reasoning_max_model_calls"] == models and config["reasoning_max_tool_calls"] == tools
    assert config["agent_turn_workers"] == 16 and config["vlm_model"] == "qwen3.8-flash"
    assert config["vlm_base_url"].startswith("https://")
    api_pid, eval_pid = pid(name, "api"), pid(name, "eval")
    assert api_pid > 0 and eval_pid > 0
    env_before = env.read_text()
    runtime_before = runtime.read_bytes()
    (folder / "api-env-before.txt").write_text(env_before)
    (folder / "api-env-before.txt").chmod(0o600)
    (folder / "runtime-before.json").write_bytes(runtime_before)
    receipt = {
        "at": time.time(),
        "run_id": run,
        "api_pid": api_pid,
        "eval_pid": eval_pid,
        "control": before,
        "runtime_configuration": config,
        "runtime_sha256": digest(runtime),
        "env_sha256": digest(env),
    }
    save(folder / "before.json", receipt)
    save(control, {"concurrency": 16, "pause_after_completed": 1})
    deadline = time.monotonic() + 7200
    stable = 0
    while time.monotonic() < deadline:
        live = get(evaluation, f"runs/{run}?include_history=false")
        assert live["run_id"] == run and live.get("fatal_error") is None
        assert pid(name, "api") == api_pid and pid(name, "eval") == eval_pid
        with sqlite3.connect((RAM / name / "sentrix.db").as_uri() + "?mode=ro", uri=True, timeout=30) as db:
            active = db.execute(
                "SELECT COUNT(*) FROM agent_turns WHERE status IN ('pending','running')"
            ).fetchone()[0]
        phase = live["phases"]["qa_eval"]
        status = {
            "at": time.time(),
            "status": "draining",
            "active_product_turns": active,
            "executed": len(phase["agent_executed_qa_ids"]),
            "progress": phase["progress"],
        }
        save(folder / "progress.json", status)
        stable = stable + 1 if drained(live, active) else 0
        if stable >= 2:
            break
        time.sleep(15)
    assert stable >= 2, "Drain deadline reached; admission remains paused for inspection"
    save(folder / "drained.json", status)
    assert env.read_text() == env_before and runtime.read_bytes() == runtime_before
    env.write_text(context_env(env_before))
    env.chmod(0o600)
    connection = json.loads(runtime_before)
    settings = connection["product_settings"]
    assert settings["vlm_context_tokens"] == settings["vlm_reasoning_context_tokens"] == 16384
    settings["vlm_context_tokens"] = settings["vlm_reasoning_context_tokens"] = CONTEXT
    save(runtime, connection)
    save(
        folder / "restart-intent.json",
        {
            "at": time.time(),
            "drained": status,
            "api_unit": unit(name, "api"),
            "new_context": CONTEXT,
            "env_sha256": digest(env),
            "runtime_sha256": digest(runtime),
        },
    )
    subprocess.run(["systemctl", "--user", "restart", unit(name, "api")], check=True, timeout=180)
    for _ in range(60):
        try:
            observed = get(port, "diagnostics")["runtime_configuration"]
            if observed["vlm_context_tokens"] == observed["vlm_reasoning_context_tokens"] == CONTEXT:
                break
        except (OSError, ValueError):
            pass
        time.sleep(5)
    else:
        raise RuntimeError("128K runtime not observed; admission remains paused")
    assert {
        k: v for k, v in observed.items() if k not in ("vlm_context_tokens", "vlm_reasoning_context_tokens")
    } == {k: v for k, v in config.items() if k not in ("vlm_context_tokens", "vlm_reasoning_context_tokens")}
    assert pid(name, "eval") == eval_pid and pid(name, "api") != api_pid
    live = get(evaluation, f"runs/{run}?include_history=false")
    assert live["run_id"] == run and live.get("fatal_error") is None
    save(
        folder / "effective.json",
        {
            "at": time.time(),
            "runtime_configuration": observed,
            "new_api_pid": pid(name, "api"),
            "eval_pid_preserved": eval_pid,
            "last_16k_completed": live["phases"]["qa_eval"]["progress"]["agent_completed"],
            "run_id": run,
            "agent_source_unchanged": True,
        },
    )
    save(control, before)
    save(
        folder / "terminal.json",
        {
            "at": time.time(),
            "status": "completed",
            "context_tokens": CONTEXT,
            "admission_restored": before,
            "run_id": run,
        },
    )
    return name, "completed"


def main(root):
    assert BASE.is_dir() and RAM.is_dir()
    assert not root.exists(), "Read existing stage; do not repeat a switch"
    root.mkdir()
    save(root / "started.json", {"at": time.time(), "target_context": CONTEXT, "groups": list(GROUPS)})
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(switch, name, root): name for name in GROUPS}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                future.result()
                results[name] = {"status": "completed"}
            except Exception as error:
                results[name] = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
                save(root / (name + "-attention.json"), results[name])
            save(root / "groups-terminal.json", results)
    save(
        root / "terminal.json",
        {
            "at": time.time(),
            "status": "completed"
            if all(v["status"] == "completed" for v in results.values())
            else "needs_review",
            "groups": results,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    main(parser.parse_args().output)
