"""Retry only frozen account-failure QA versions, preserving the live full run."""

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from representative_qa import get, load, process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
ROOT = B / "planner-account8-retry-20261127"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"


def prepare(root, group="planner"):
    baseline = group == "baseline"
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
    from photobench.infrastructure.results.store import FileRunStore

    assert not root.exists(), "Existing retry must be inspected and reused"
    original = B / "planner-large-continue16-20261127"
    g = json.loads(
        (
            (B / "full-four-continue-20261127/prepared/baseline-large/started.json")
            if baseline
            else (original / "prepared/planner-large/started.json")
        ).read_text()
    )
    audit = json.loads(
        (
            original
            / "quality-review"
            / ("baseline-large-items.json" if baseline else "planner-large-items.json")
        ).read_text()
    )
    selected = [
        r
        for r in audit
        if r["final_signals"]
        or (
            baseline
            and (
                r["signals"]
                and "model_error" in r["termination"]
                or not r["signals"]
                and "ModelServiceError" in r["error_types"]
                and any("The read operation timed out" in e for e in r["errors"])
            )
        )
    ]
    count = 27 if baseline else 8
    assert len(selected) == count
    assert baseline or all(r["score"] == 0 for r in selected)
    source = FileRunStore(Path(g["sources"][0]["results"])).load_state(g["source_run"])
    snapshot = copy.deepcopy(source["qa_snapshot"])
    ids = {r["qa_id"] for r in selected}
    snapshot["rows"] = [r for r in snapshot["rows"] if r["qa_id"] in ids]
    assert len(snapshot["rows"]) == count
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    config = get(g["product"], "diagnostics")["runtime_configuration"]
    assert config["agent_turn_workers"] == 16
    assert config["reasoning_max_model_calls"] == 100
    assert config["reasoning_max_tool_calls"] == 600
    assert config["vlm_context_tokens"] == 131072
    # The independent evaluator adds two queued requests; product workers remain capped at16.
    root.mkdir()
    save(root / "snapshot.json", snapshot)
    save(
        root / "selection.json",
        {
            "items": selected,
            "count": count,
            "source_run": g["source_run"],
            "definition_sha256": snapshot["definition_sha256"],
            "old_scores_preserved": True,
            "at": time.time(),
        },
    )
    g["qa_count"] = count
    g["missing_ids"] = [r["qa_id"] for r in snapshot["rows"]]
    save(
        root / "intent.json",
        {
            "group": g,
            "score_root": str(B / "representative83-priority-20261127"),
            "scope": "baseline27servicefailure" if baseline else "only8accountfailure",
            "qa_concurrency": 2,
            "product_workers": 16,
            "judge_concurrency": 2,
            "original_full_run_unchanged": True,
            "at": time.time(),
        },
    )
    print(json.dumps({"selected": count, "product_workers": 16}))


def worker(root):
    from photobench.application.run_execution.service import RunRequest
    from photobench.bootstrap.container import Container
    from photobench.config import Settings
    from photobench.domain.runtime.ports import RunRuntimeSnapshot

    plan = json.loads((root / "intent.json").read_text())
    g = plan["group"]
    folder = root
    score_root = Path(plan["score_root"])
    snapshot = json.loads((root / "snapshot.json").read_text())
    live = get(g["port"], f"runs/{g['source_run']}?include_history=false")
    settings = Settings(
        config_root=Path(os.environ["PHOTOBENCH_CONFIG_ROOT"]),
        results_root=folder / "results",
        data_root=Path(os.environ["PHOTOBENCH_DATA_ROOT"]),
        sentrix_repo_root=Path(g["evaluation_source"]).parents[2],
        sentrix_api_url=f"http://127.0.0.1:{g['product']}",
        qa_concurrency=2,
        sentrix_service_unit=os.environ["PHOTOBENCH_SENTRIX_SERVICE_UNIT"],
        sentrix_start_command=os.environ["PHOTOBENCH_SENTRIX_START_COMMAND"],
        sentrix_pid_file=Path(os.environ["PHOTOBENCH_SENTRIX_PID_FILE"]),
    )
    container = Container.build(settings)
    connection = container.config.public_runtime_connection_config()
    runtime = RunRuntimeSnapshot(json.dumps(connection), container.build_revision)
    run = f"{root.name}-{int(time.time())}"
    request = RunRequest(
        album_id=live["album_id"],
        qa_set=live["qa_set"],
        model_profile=live["model_profile"],
        sentrix_url=f"http://127.0.0.1:{g['product']}",
        model_base_url=connection["model_base_url"],
        run_id=run,
        mode="reuse",
        scope_id=live["scope_id"],
        frozen_qa_snapshot=snapshot,
        qa_concurrency=2,
        reasoning=live["reasoning"],
        graph_enabled=live["graph_enabled"],
        defer_scoring=True,
        turn_timeout=3660,
        runtime_snapshot=runtime,
        resume_context={"source_run_id": g["source_run"], "reason": plan["scope"]},
    )
    save(
        folder / "started.json",
        {
            "run_id": run,
            "source_run_id": g["source_run"],
            "qa_count": len(snapshot["rows"]),
            "qa_ids": g["missing_ids"],
            "at": time.time(),
            "context_tokens": 131072,
        },
    )
    if snapshot["rows"]:
        execution = container.run_execution(request)
        diagnostics = execution.client.diagnostics
        execution.client.diagnostics = lambda **kw: diagnostics(timeout=60)
        result = execution.execute(request)
        assert result["status"] in {"completed", "completed_with_errors"}, result.get("fatal_error")
        source = container.run_store.load_state(run)
        save(
            folder / "qa-terminal.json",
            {
                "status": result["status"],
                "qa_count": len(source["items"]),
                "progress": result["phases"]["qa_eval"]["progress"],
            },
        )
    else:
        source = {"run_id": run, "qa_snapshot": snapshot, "items": []}
    score_existing(root, source, g["qa_count"], score_root)


def score_existing(root, source, qa_count, score_root):
    name = root.name
    folder = root
    previous = json.loads((score_root / "intent.json").read_text())
    # Use the same frozen production scoring protocol and audited direct HTTP adapter.
    follow = load("representative_follow", previous["follow_source"])
    scoring = folder / "judge"
    scoring.mkdir()
    (scoring / "intent.json").write_bytes((score_root / "intent.json").read_bytes())
    (scoring / "protocol.json").write_bytes((score_root / "protocol.json").read_bytes())
    owner = follow.Follow(scoring)
    import shlex

    key = next(
        shlex.split(line.split("=", 1)[1])[0]
        for line in Path(previous["secret_env"]).read_text().splitlines()
        if line.startswith("QWEN_API_KEY=")
    )
    os.environ.update(
        QWEN_API_KEY=key,
        FLASH_DIRECT_URL=owner.protocol["url"],
        FLASH_DIRECT_HTTP_DIR=str(scoring / "http/raw"),
    )
    load("representative_transport", previous["transport_source"]).install()
    from photobench.application.run_execution.judge import JudgeRunner

    runner = JudgeRunner(
        http=owner.http(name),
        judge_url=owner.protocol["url"],
        judge_model=owner.protocol["model"],
        judge_api_key=key,
        cancel=__import__("threading").Event(),
        evaluation_options=owner.protocol["evaluation_options"],
    )
    source["items"] = [v for v in source["items"] if v.get("execution_status") == "completed"]
    save(scoring / "input.json", source)
    status = owner.scorer.score_saved(source, owner.protocol, scoring, runner, concurrency=2)
    save(
        folder / "terminal.json",
        {"status": status["status"], "qa_count": qa_count, "scoring": status, "at": time.time()},
    )


def launch(root):
    assert not (root / "launch.json").exists()
    plan = json.loads((root / "intent.json").read_text())
    g = plan["group"]
    _, _, env = process_environment(g["port"])
    env["PYTHONPATH"] = g["evaluation_source"] + ":" + env.get("PYTHONPATH", "")
    log = (root / "worker.log").open("x")
    proc = subprocess.Popen(
        [sys.executable, __file__, "--worker", str(root)],
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    save(root / "launch.json", {"pid": proc.pid, "at": time.time()})
    print(json.dumps({"pid": proc.pid, "selected": g["qa_count"]}))


def status(root):
    from photobench.infrastructure.results.store import FileRunStore

    result = {"status": "running", "at": time.time()}
    for name in ["failure", "terminal"]:
        if (root / (name + ".json")).exists():
            result[name] = json.loads((root / (name + ".json")).read_text())
            result["status"] = (
                "completed"
                if name == "terminal" and result[name]["status"] == "completed"
                else "needs_review"
            )
    started = root / "started.json"
    if started.exists():
        s = json.loads(started.read_text())
        result["run_id"] = s["run_id"]
        try:
            state = FileRunStore(root / "results").load_state(s["run_id"])
            result["processed"] = len(state["items"])
            result["qa_status"] = state["status"]
        except KeyError:
            result["qa_status"] = "starting"
    launch_file = root / "launch.json"
    if launch_file.exists():
        pid = json.loads(launch_file.read_text())["pid"]
        result["pid"] = pid
        result["alive"] = Path(f"/proc/{pid}").exists()
        if not result["alive"] and result["status"] == "running":
            result["status"] = "needs_review"
    return result


def watch(output):
    remote_root = B / output.name
    output.mkdir(parents=True, exist_ok=True)
    while True:
        response = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "192.168.0.100",
                (
                    f"PYTHONPATH={B}/flash-dual-baseline-20261126/release/apps/photobench/src "
                    f"{PYTHON} {remote_root}/account_retry.py --status {remote_root}"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if response.returncode:
            save(output / "observer-error.json", {"stderr": response.stderr[-1000:]})
            return 1
        data = json.loads(response.stdout)
        save(output / "latest.json", data)
        if data["status"] != "running":
            save(output / "terminal.json", data)
            return 0 if data["status"] == "completed" else 1
        time.sleep(30)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--prepare", type=Path)
    p.add_argument("--group", choices=["planner", "baseline"], default="planner")
    p.add_argument("--launch", type=Path)
    p.add_argument("--worker", type=Path)
    p.add_argument("--status", type=Path)
    p.add_argument("--watch", type=Path)
    a = p.parse_args()
    if a.prepare:
        prepare(a.prepare, a.group)
    elif a.launch:
        launch(a.launch)
    elif a.worker:
        try:
            worker(a.worker)
        except Exception as exc:
            save(
                a.worker / "failure.json", {"status": "failed", "error": str(exc), "type": type(exc).__name__}
            )
            raise
    elif a.status:
        print(json.dumps(status(a.status)))
    else:
        raise SystemExit(watch(a.watch))
