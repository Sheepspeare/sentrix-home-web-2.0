"""Run only unanswered representative UUIDs in isolated evaluator result directories."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=60) as r:
        return json.load(r)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process_environment(port):
    matches = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            words = (proc / "cmdline").read_bytes().decode().split("\0")
            if "uvicorn" in words and "--port" in words and words[words.index("--port") + 1] == str(port):
                env = dict(
                    v.split("=", 1) for v in (proc / "environ").read_bytes().decode().split("\0") if "=" in v
                )
                matches.append((int(proc.name), words, env))
        except (OSError, UnicodeError):
            continue
    assert len(matches) == 1, (port, len(matches))
    return matches[0]


def missing_rows(snapshot, ids, present):
    assert len(ids) == len(set(ids)) == 83
    rows = [copy.deepcopy(r) for r in snapshot["rows"] if r["qa_id"] in ids and r["qa_id"] not in present]
    assert {r["qa_id"] for r in rows} == set(ids) - set(present)
    return rows


def prepare(root, score_root):
    assert not root.exists()
    root.mkdir()
    prior = json.loads((score_root / "intent.json").read_text())
    groups = []
    for old in prior["groups"]:
        name, port, run = old["name"], old["port"], old["run_id"]
        live = get(port, f"runs/{run}?include_history=false")
        phase = live["phases"]["qa_eval"]
        assert phase["progress"]["dispatch_paused"]
        assert (
            len(phase["agent_executed_qa_ids"]) + int(phase.get("agent_inherited_count", 0))
            == phase["progress"]["agent_completed"]
        )
        present, page = {}, 1
        while True:
            listing = get(port, f"runs/{run}/items?page={page}&page_size=100")
            present.update(
                {v["qa_id"]: v for v in listing["items"] if v["qa_id"] in prior["representative_ids"]}
            )
            if not listing.get("has_next"):
                break
            page += 1
        product = {"limited": 8305, "large": 8306, "fusion": 8303}[name]
        _, _, product_env = process_environment(product)
        db_path = Path(product_env["SENTRIX_DATA_DIR"]) / "sentrix.db"
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
            active = db.execute(
                "select count(*) from agent_turns where status in ('pending','running')"
            ).fetchone()[0]
        assert active == 0, (name, active)
        config = get(product, "diagnostics")["runtime_configuration"]
        assert config["vlm_context_tokens"] == config["vlm_reasoning_context_tokens"] == 131072
        assert config["reasoning_max_model_calls"] == (20 if name == "limited" else 100)
        assert config["reasoning_max_tool_calls"] == (30 if name == "limited" else 600)
        assert config["vlm_base_url"].startswith("https://")
        snapshot = json.loads(Path(old["previous_states"][0]["path"]).read_text())["qa_snapshot"]
        rows = missing_rows(snapshot, prior["representative_ids"], present)
        pid, words, _ = process_environment(port)
        groups.append(
            {
                "name": name,
                "port": port,
                "product": product,
                "source_run": run,
                "eval_pid": pid,
                "evaluation_source": words[words.index("--app-dir") + 1],
                "missing_ids": [r["qa_id"] for r in rows],
                "present_ids": list(present),
                "db_path": str(db_path),
                "qa_count": len(rows),
            }
        )
    save(
        root / "intent.json",
        {
            "score_root": str(score_root),
            "groups": groups,
            "full_dispatch_remains_paused": True,
            "qa_concurrency": 16,
            "judge_concurrency": 2,
            "created_at": time.time(),
        },
    )
    print(json.dumps({"groups": [{k: g[k] for k in ["name", "qa_count"]} for g in groups]}))


def worker(root, name):
    plan = json.loads((root / "intent.json").read_text())
    g = next(g for g in plan["groups"] if g["name"] == name)
    folder = root / name
    folder.mkdir()
    assert get(g["port"], f"runs/{g['source_run']}?include_history=false")["phases"]["qa_eval"]["progress"][
        "dispatch_paused"
    ]
    with sqlite3.connect(Path(g["db_path"]).as_uri() + "?mode=ro", uri=True) as db:
        assert (
            db.execute("select count(*) from agent_turns where status in ('pending','running')").fetchone()[0]
            == 0
        )
    from photobench.application.run_execution.service import RunRequest
    from photobench.bootstrap.container import Container
    from photobench.config import Settings
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
    from photobench.domain.runtime.ports import RunRuntimeSnapshot

    score_root = Path(plan["score_root"])
    previous = json.loads((score_root / "intent.json").read_text())
    old = next(v for v in previous["groups"] if v["name"] == name)
    snapshot = json.loads(Path(old["previous_states"][0]["path"]).read_text())["qa_snapshot"]
    snapshot["rows"] = missing_rows(snapshot, previous["representative_ids"], set(g["present_ids"]))
    assert [r["qa_id"] for r in snapshot["rows"]] == g["missing_ids"]
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    live = get(g["port"], f"runs/{g['source_run']}?include_history=false")
    settings = Settings(
        config_root=Path(os.environ["PHOTOBENCH_CONFIG_ROOT"]),
        results_root=folder / "results",
        data_root=Path(os.environ["PHOTOBENCH_DATA_ROOT"]),
        sentrix_repo_root=Path(g["evaluation_source"]).parents[2],
        sentrix_api_url=f"http://127.0.0.1:{g['product']}",
        qa_concurrency=16,
    )
    container = Container.build(settings)
    connection = container.config.public_runtime_connection_config()
    runtime = RunRuntimeSnapshot(json.dumps(connection), container.build_revision)
    run = f"representative83-{name}-{int(time.time())}"
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
        qa_concurrency=16,
        reasoning=live["reasoning"],
        graph_enabled=live["graph_enabled"],
        defer_scoring=True,
        turn_timeout=3660,
        runtime_snapshot=runtime,
        resume_context={"source_run_id": g["source_run"], "reason": "only_unanswered_representative83"},
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
        result = container.run_execution(request).execute(request)
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
        {"status": status["status"], "qa_count": g["qa_count"], "scoring": status, "at": time.time()},
    )


def launch(root):
    plan = json.loads((root / "intent.json").read_text())
    assert not (root / "launch.json").exists()
    children = []
    for g in plan["groups"]:
        _, _, env = process_environment(g["port"])
        env["PYTHONPATH"] = g["evaluation_source"] + ":" + env.get("PYTHONPATH", "")
        log = (root / (g["name"] + ".log")).open("x")
        p = subprocess.Popen(
            [sys.executable, __file__, str(root), "--worker", g["name"]], env=env, stdout=log, stderr=log
        )
        children.append((g["name"], p))
    save(root / "launch.json", {"at": time.time(), "pids": {name: p.pid for name, p in children}})
    outcomes = {name: p.wait() for name, p in children}
    good = all(v == 0 for v in outcomes.values())
    save(
        root / "terminal.json",
        {"status": "completed" if good else "needs_review", "exit_codes": outcomes, "at": time.time()},
    )
    return 0 if good else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--worker")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.root, args.prepare)
    elif args.worker:
        try:
            worker(args.root, args.worker)
        except Exception as error:
            save(
                args.root / args.worker / "failure.json", {"type": type(error).__name__, "error": str(error)}
            )
            raise
    else:
        raise SystemExit(launch(args.root))
