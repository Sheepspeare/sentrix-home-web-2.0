"""Continue four frozen experiments using disjoint missing-only QA snapshots."""

import argparse
import copy
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from representative_qa import get, load, process_environment, save

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
SCORE = BASE / "representative83-priority-20261127"
SPECS = [
    ("baseline-limited", 8885, 8305, "20261126-225652-2026.11.17-atm-sources-addresses-current-801a7b", 16),
    ("baseline-large", 8886, 8306, "20261126-225652-2026.11.17-atm-sources-addresses-current-2db84c", 16),
    ("improved-limited", 8884, 8304, "20261127-014314-2026.11.17-atm-sources-addresses-big_model-35dfdb", 8),
    (
        "improved-large",
        8883,
        8303,
        "20261127-085925-2026.11.17-atm-sources-addresses-big_model-resume-c726ac",
        16,
    ),
]


def select_missing(snapshot, present):
    ids = [row["qa_id"] for row in snapshot["rows"]]
    assert len(ids) == len(set(ids)) == 1013
    assert set(present) <= set(ids)
    return [copy.deepcopy(row) for row in snapshot["rows"] if row["qa_id"] not in present]


def idle(product):
    _, _, env = process_environment(product)
    path = Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        count = db.execute(
            "select count(*) from agent_turns where status in ('pending','running')"
        ).fetchone()[0]
    assert count == 0, (product, count)


def prepare(root):
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
    from photobench.infrastructure.results.store import FileRunStore

    root.mkdir()
    groups = []
    for name, port, product, run, concurrency in SPECS:
        _, words, env = process_environment(port)
        live = get(port, f"runs/{run}?include_history=false")
        assert live["phases"]["qa_eval"]["progress"]["dispatch_paused"]
        idle(product)
        conf = get(product, "diagnostics")["runtime_configuration"]
        assert conf["vlm_context_tokens"] == conf["vlm_reasoning_context_tokens"] == 131072
        assert conf["reasoning_max_model_calls"] == (20 if name.endswith("limited") else 100)
        assert conf["reasoning_max_tool_calls"] == (30 if name.endswith("limited") else 600)
        store = FileRunStore(Path(env["PHOTOBENCH_RESULTS_ROOT"]))
        state = store.load_state(run)
        present = {v["qa_id"] for v in state["items"]}
        sources = [{"results": env["PHOTOBENCH_RESULTS_ROOT"], "run": run, "count": len(present)}]
        if name.startswith("baseline"):
            folder = BASE / "representative83-missing-qa-20261127-v7" / name.split("-")[1]
        elif name == "improved-large":
            folder = BASE / "fusion83-account-retry-20261127/fusion"
        else:
            folder = None
        if folder:
            extra_run = json.loads((folder / "started.json").read_text())["run_id"]
            extra = FileRunStore(folder / "results").load_state(extra_run)
            assert extra.get("finished_at"), (name, "supplement not finished")
            extra_ids = {v["qa_id"] for v in extra["items"]}
            sources.append(
                {
                    "results": str(folder / "results"),
                    "run": extra_run,
                    "count": len(extra_ids),
                    "overrides_original": name == "improved-large",
                }
            )
            present |= extra_ids
        snapshot = copy.deepcopy(state["qa_snapshot"])
        rows = select_missing(snapshot, present)
        snapshot["rows"] = rows
        snapshot["definition_sha256"] = qa_fingerprint(rows)
        folder = root / name
        folder.mkdir()
        save(folder / "snapshot.json", snapshot)
        save(
            folder / "inherited-manifest.json",
            {"qa_ids": sorted(present), "sources": sources, "frozen_total": 1013},
        )
        groups.append(
            {
                "name": name,
                "port": port,
                "product": product,
                "source_run": run,
                "evaluation_source": words[words.index("--app-dir") + 1],
                "concurrency": concurrency,
                "inherited": len(present),
                "remaining": len(rows),
                "sources": sources,
            }
        )
    save(
        root / "intent.json",
        {
            "groups": groups,
            "score_root": str(SCORE),
            "originals_remain_paused": True,
            "judge_concurrency_per_group": 2,
            "at": time.time(),
        },
    )
    print(
        json.dumps(
            [{"name": g["name"], "inherited": g["inherited"], "remaining": g["remaining"]} for g in groups]
        )
    )


def judge(root, g, finished):
    from photobench.application.run_execution.judge import JudgeRunner
    from photobench.infrastructure.results.store import FileRunStore

    folder = root / g["name"]
    scoring = folder / "judge"
    scoring.mkdir()
    previous = json.loads((SCORE / "intent.json").read_text())
    (scoring / "intent.json").write_bytes((SCORE / "intent.json").read_bytes())
    (scoring / "protocol.json").write_bytes((SCORE / "protocol.json").read_bytes())
    owner = load("continue_follow", previous["follow_source"]).Follow(scoring)
    key = next(
        shlex.split(s.split("=", 1)[1])[0]
        for s in Path(previous["secret_env"]).read_text().splitlines()
        if s.startswith("QWEN_API_KEY=")
    )
    os.environ.update(
        QWEN_API_KEY=key,
        FLASH_DIRECT_URL=owner.protocol["url"],
        FLASH_DIRECT_HTTP_DIR=str(scoring / "http/raw"),
    )
    load("continue_transport", previous["transport_source"]).install()
    runner = JudgeRunner(
        http=owner.http(g["name"]),
        judge_url=owner.protocol["url"],
        judge_model=owner.protocol["model"],
        judge_api_key=key,
        cancel=threading.Event(),
        evaluation_options=owner.protocol["evaluation_options"],
    )
    store = FileRunStore(folder / "results")
    previous_group = {
        "baseline-limited": "limited",
        "baseline-large": "large",
        "improved-large": "fusion",
    }.get(g["name"])
    score_paths = [Path(v) for v in g.get("previous_score_paths", [])]
    if previous_group:
        original = next(v for v in previous["groups"] if v["name"] == previous_group)
        score_paths.extend(Path(v["path"]) for v in original["previous_states"])
        score_paths.append(SCORE / previous_group / "scoring-state.json")
    if g["name"].startswith("baseline"):
        old_folder = BASE / "representative83-missing-qa-20261127-v7" / previous_group
        score_paths.extend(sorted(old_folder.glob("judge*/batch-*/scoring-state.json")))
    elif g["name"] == "improved-large":
        score_paths.extend(
            sorted((BASE / "fusion83-account-retry-20261127/fusion/judge").glob("batch-*/scoring-state.json"))
        )
    elif not score_paths:
        score_paths.append(root / "improved-limited-existing-scores.json")
    preserved = {}
    for path in score_paths:
        saved = json.loads(path.read_text())
        for value in saved.get("scoring_results", {}).values():
            if value.get("status") == "completed":
                diagnostic = saved.get("diagnostic_results", {}).get(value.get("diagnostic_key"), {})
                assert diagnostic.get("status") == "completed"
                preserved[value["qa_id"]] = {"path": str(path), "result": value}
    scored = set(preserved)
    save(scoring / "preserved-scores.json", preserved)
    inherited_items = {}
    inherited_snapshot = None
    for origin in g["sources"]:
        original_state = FileRunStore(Path(origin["results"])).load_state(origin["run"])
        if inherited_snapshot is None:
            inherited_snapshot = original_state["qa_snapshot"]
        for item in original_state["items"]:
            inherited_items[item["qa_id"]] = item
    batch = 0
    while True:
        try:
            state = store.load_state(g["run_id"])
        except KeyError:
            if finished.wait(5):
                raise RuntimeError("QA ended without saved state") from None
            continue
        all_items = dict(inherited_items)
        all_items.update({v["qa_id"]: v for v in state["items"]})
        items = [
            v
            for v in all_items.values()
            if v.get("execution_status") == "completed" and v["qa_id"] not in scored
        ]
        if items:
            batch += 1
            out = scoring / f"batch-{batch:04d}"
            out.mkdir()
            source = {"run_id": g["run_id"], "qa_snapshot": inherited_snapshot, "items": items[:16]}
            save(out / "input.json", source)
            result = owner.scorer.score_saved(source, owner.protocol, out, runner, concurrency=2)
            saved = json.loads((out / "scoring-state.json").read_text())
            scored.update(
                v["qa_id"]
                for v in saved.get("scoring_results", {}).values()
                if v.get("status") == "completed"
                and saved.get("diagnostic_results", {}).get(v.get("diagnostic_key"), {}).get("status")
                == "completed"
            )
            save(
                scoring / "progress.json",
                {
                    "valid_total": len(scored),
                    "qa_new_processed": len(state["items"]),
                    "qa_new_total": g["remaining"],
                    "inherited_processed": g["inherited"],
                    "at": time.time(),
                },
            )
            if result["status"] != "completed":
                save(scoring / "terminal.json", dict(result, valid_new=len(scored)))
                return
            continue
        if state.get("finished_at"):
            save(
                scoring / "terminal.json",
                {
                    "status": "completed",
                    "valid_total": len(scored),
                    "qa_new_processed": len(state["items"]),
                    "unscorable": sum(v.get("execution_status") != "completed" for v in state["items"]),
                    "at": time.time(),
                },
            )
            return
        time.sleep(15)


def worker(root, name):
    from photobench.application.run_execution.service import RunRequest
    from photobench.bootstrap.container import Container
    from photobench.config import Settings
    from photobench.domain.runtime.ports import RunRuntimeSnapshot

    g = next(v for v in json.loads((root / "intent.json").read_text())["groups"] if v["name"] == name)
    folder = root / name
    idle(g["product"])
    live = get(g["port"], f"runs/{g['source_run']}?include_history=false")
    assert live["phases"]["qa_eval"]["progress"]["dispatch_paused"]
    settings = Settings(
        config_root=Path(os.environ["PHOTOBENCH_CONFIG_ROOT"]),
        results_root=folder / "results",
        data_root=Path(os.environ["PHOTOBENCH_DATA_ROOT"]),
        sentrix_repo_root=Path(g["evaluation_source"]).parents[2],
        sentrix_api_url=f"http://127.0.0.1:{g['product']}",
        qa_concurrency=g["concurrency"],
        sentrix_service_unit=os.environ["PHOTOBENCH_SENTRIX_SERVICE_UNIT"],
        sentrix_start_command=os.environ["PHOTOBENCH_SENTRIX_START_COMMAND"],
        sentrix_pid_file=Path(os.environ["PHOTOBENCH_SENTRIX_PID_FILE"]),
    )
    container = Container.build(settings)
    connection = container.config.public_runtime_connection_config()
    g["run_id"] = f"full-continue-{name}-{int(time.time())}"
    snapshot = json.loads((folder / "snapshot.json").read_text())
    request = RunRequest(
        album_id=live["album_id"],
        qa_set=live["qa_set"],
        model_profile=live["model_profile"],
        sentrix_url=f"http://127.0.0.1:{g['product']}",
        model_base_url=connection["model_base_url"],
        run_id=g["run_id"],
        mode="reuse",
        scope_id=live["scope_id"],
        frozen_qa_snapshot=snapshot,
        qa_concurrency=g["concurrency"],
        reasoning=live["reasoning"],
        graph_enabled=live["graph_enabled"],
        defer_scoring=True,
        turn_timeout=3660,
        runtime_snapshot=RunRuntimeSnapshot(json.dumps(connection), container.build_revision),
        resume_context={
            "source_run_id": g["source_run"],
            "reason": "full_missing_only_after_representative83",
            "inherited_manifest": str(folder / "inherited-manifest.json"),
        },
    )
    save(folder / "started.json", dict(g, pid=os.getpid(), at=time.time()))
    judge_log = (folder / "judge.log").open("x")
    judge_process = subprocess.Popen(
        [sys.executable, __file__, str(root), "--judge", name],
        stdout=judge_log,
        stderr=judge_log,
        start_new_session=True,
    )
    save(folder / "judge-process.json", {"pid": judge_process.pid, "concurrency": 2})
    try:
        result = container.run_execution(request).execute(request)
        save(
            folder / "qa-terminal.json",
            {
                "status": result["status"],
                "progress": result.get("phases", {}).get("qa_eval", {}).get("progress"),
                "error": result.get("fatal_error"),
            },
        )
    except Exception:
        raise
    judge_process.wait()
    save(
        folder / "terminal.json",
        {
            "status": "completed"
            if result["status"] in ["completed", "completed_with_errors"]
            and not (folder / "judge-failure.json").exists()
            and json.loads((folder / "judge/terminal.json").read_text())["status"] == "completed"
            else "needs_review",
            "at": time.time(),
        },
    )


def launch(root):
    plan = json.loads((root / "intent.json").read_text())
    assert not (root / "launch.json").exists()
    children = {}
    for g in plan["groups"]:
        _, _, env = process_environment(g["port"])
        env["PYTHONPATH"] = g["evaluation_source"] + ":" + env.get("PYTHONPATH", "")
        log = (root / (g["name"] + ".log")).open("x")
        child = subprocess.Popen(
            [sys.executable, __file__, str(root), "--worker", g["name"]],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        children[g["name"]] = child.pid
    save(root / "launch.json", {"pids": children, "at": time.time()})
    print(json.dumps(children))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--worker")
    p.add_argument("--judge")
    a = p.parse_args()
    if a.prepare:
        prepare(a.root)
    elif a.judge:
        g = json.loads((a.root / a.judge / "started.json").read_text())
        try:
            judge(a.root, g, threading.Event())
        except Exception as exc:
            save(
                a.root / a.judge / "judge-failure.json",
                {"status": "failed", "error": str(exc), "type": type(exc).__name__},
            )
            raise
    elif a.worker:
        try:
            worker(a.root, a.worker)
        except Exception as exc:
            save(
                a.root / a.worker / "failure.json",
                {"status": "failed", "type": type(exc).__name__, "error": str(exc)},
            )
            raise
    else:
        launch(a.root)
