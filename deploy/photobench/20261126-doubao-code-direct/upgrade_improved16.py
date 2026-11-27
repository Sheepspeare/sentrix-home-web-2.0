"""Drain and preserve the eight-worker evaluation before missing-only continuation."""

import argparse
import copy
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
ROOT = B / "improved-limited-continue16-20261127"
OLD = B / "full-four-continue-20261127/prepared"
DEPLOY = B / "flash-condition-fusion-limited-20261127"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"
SOURCE = DEPLOY / "release/apps/photobench/src"


def apply():
    from photobench.application.run_execution.service import RunRequest, index_assets_by_name
    from photobench.bootstrap.container import Container
    from photobench.config import Settings
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
    from photobench.domain.runtime.ports import RunRuntimeSnapshot
    from photobench.infrastructure.results.store import FileRunStore

    assert not (ROOT / "intent.json").exists()
    old_folder = OLD / "improved-limited"
    g = json.loads((old_folder / "started.json").read_text())
    pid = g["pid"]
    assert "continue_four.py" in Path(f"/proc/{pid}/cmdline").read_bytes().decode()
    save(
        ROOT / "intent.json",
        {
            "old_run": g["run_id"],
            "old_pid": pid,
            "qa_concurrency": 16,
            "judge_concurrency": 2,
            "at": time.time(),
        },
    )
    os.kill(pid, signal.SIGSTOP)
    for _ in range(30):
        if Path(f"/proc/{pid}/stat").read_text().split(") ")[1].split()[0] == "T":
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Old dispatcher pause not observed")
    save(old_folder / "intentional-pause.json", {"reason": "authorized_upgrade16", "pid": pid})
    old_state = FileRunStore(old_folder / "results").load_state(g["run_id"])
    present = {v["qa_id"] for v in old_state["items"]}
    executed = set(old_state["phases"]["qa_eval"]["agent_executed_qa_ids"])
    outstanding = executed - present
    _, _, product_env = process_environment(8304)
    db_path = Path(product_env["SENTRIX_DATA_DIR"]) / "sentrix.db"
    mapping = {}
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(g["at"]))
        candidates = db.execute(
            "select distinct c.title from agent_turns t join agent_conversations c on c.id=t.conversation_id "
            "where julianday(t.created_at) >= julianday(?)",
            (started_at + "+00:00",),
        ).fetchall()
        definitions_ids = {r["qa_id"] for r in old_state["qa_snapshot"]["rows"]}
        outstanding |= {r[0] for r in candidates if r[0] in definitions_ids} - present
        for q in outstanding:
            matches = db.execute(
                "select t.id,t.conversation_id from agent_turns t join agent_conversations c "
                "on c.id=t.conversation_id where c.title=? order by t.created_at desc",
                (q,),
            ).fetchall()
            assert len(matches) == 1, (q, len(matches))
            mapping[q] = {"turn_id": matches[0][0], "conversation_id": matches[0][1]}
    save(ROOT / "inflight.json", mapping)
    stable = 0
    for _ in range(600):
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
            active = db.execute(
                "select count(*) from agent_turns where status in ('pending','running')"
            ).fetchone()[0]
        stable = stable + 1 if active == 0 else 0
        save(
            ROOT / "progress.json",
            {
                "status": "draining",
                "active": active,
                "preserved": len(present),
                "inflight": len(mapping),
                "at": time.time(),
            },
        )
        if stable >= 2:
            break
        time.sleep(10)
    assert stable >= 2, "Drain deadline reached; old dispatcher stays paused"
    _, _, env = process_environment(8884)
    live = get(8884, f"runs/{g['source_run']}?include_history=false")
    settings = Settings(
        config_root=Path(env["PHOTOBENCH_CONFIG_ROOT"]),
        results_root=ROOT / "salvage",
        data_root=Path(env["PHOTOBENCH_DATA_ROOT"]),
        sentrix_repo_root=Path(g["evaluation_source"]).parents[2],
        sentrix_api_url="http://127.0.0.1:8304",
        qa_concurrency=8,
        sentrix_service_unit=env["PHOTOBENCH_SENTRIX_SERVICE_UNIT"],
        sentrix_start_command=env["PHOTOBENCH_SENTRIX_START_COMMAND"],
        sentrix_pid_file=Path(env["PHOTOBENCH_SENTRIX_PID_FILE"]),
    )
    container = Container.build(settings)
    connection = container.config.public_runtime_connection_config()
    req = RunRequest(
        album_id=live["album_id"],
        qa_set=live["qa_set"],
        model_profile=live["model_profile"],
        sentrix_url="http://127.0.0.1:8304",
        model_base_url=connection["model_base_url"],
        run_id="salvage",
        mode="reuse",
        scope_id=live["scope_id"],
        reasoning=live["reasoning"],
        graph_enabled=live["graph_enabled"],
        defer_scoring=True,
        runtime_snapshot=RunRuntimeSnapshot(json.dumps(connection), container.build_revision),
    )
    execution = container.run_execution(req)
    assets = index_assets_by_name(execution.client.list_assets(scope_id=live["scope_id"], limit=None))
    definitions = {r["qa_id"]: r for r in old_state["qa_snapshot"]["rows"]}
    recovered = []
    for q, m in mapping.items():
        # Reuse the evaluator's projection with existing IDs. No conversation or turn POST occurs.
        polled = execution.client.get_turn(turn_id=m["turn_id"], timeout=60)
        assert polled["status"] not in ("running", "pending")
        execution.client.start_conversation = lambda m=m, **kw: {"id": m["conversation_id"]}
        execution.client.start_turn = lambda m=m, **kw: {"turn_id": m["turn_id"]}
        execution._await_turn = lambda *a, polled=polled, **kw: polled
        item = execution._evaluate_one(definitions[q], live["scope_id"], assets, req)
        assert item["execution_status"] == "completed"
        recovered.append(item)
    save(
        ROOT / "salvage-input.json",
        {"run_id": "salvage", "qa_snapshot": old_state["qa_snapshot"], "items": recovered},
    )
    old_state["items"].extend(recovered)
    # Save a separate evidence source; never modify the old run store.
    salvage_store = FileRunStore(ROOT / "salvage-results")
    old_state["run_id"] = "preserved-before16"
    salvage_store.write_snapshot("preserved-before16", old_state)
    present = {v["qa_id"] for v in old_state["items"]}
    conf = get(8304, "diagnostics")["runtime_configuration"]
    assert conf["agent_turn_workers"] == 8
    api_env = DEPLOY / "limited-api.env"
    text = api_env.read_text()
    assert text.count('SENTRIX_AGENT_TURN_MAX_WORKERS="8"') == 1
    (ROOT / "api-env-before.txt").write_text(text)
    (ROOT / "api-env-before.txt").chmod(0o600)
    api_env.write_text(
        text.replace('SENTRIX_AGENT_TURN_MAX_WORKERS="8"', 'SENTRIX_AGENT_TURN_MAX_WORKERS="16"')
    )
    api_env.chmod(0o600)
    runtime = Path(env["PHOTOBENCH_CONFIG_ROOT"]) / "runtime_connection.json"
    config = json.loads(runtime.read_text())
    save(ROOT / "runtime-before.json", config)
    config["product_settings"]["agent_turn_workers"] = 16
    save(runtime, config)
    subprocess.run(
        ["systemctl", "--user", "restart", "sentrix-flash-condition-fusion-limited83-limited-api.service"],
        check=True,
        timeout=180,
    )
    for _ in range(60):
        try:
            after = get(8304, "diagnostics")["runtime_configuration"]
            if after["agent_turn_workers"] == 16:
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        raise RuntimeError("Worker16 not verified")
    assert {k: v for k, v in conf.items() if k != "agent_turn_workers"} == {
        k: v for k, v in after.items() if k != "agent_turn_workers"
    }
    sources = g["sources"] + [
        {"results": str(ROOT / "salvage-results"), "run": "preserved-before16", "count": len(present)}
    ]
    all_present = set(present)
    for src in g["sources"]:
        all_present.update(
            v["qa_id"] for v in FileRunStore(Path(src["results"])).load_state(src["run"])["items"]
        )
    snapshot = copy.deepcopy(
        FileRunStore(Path(g["sources"][0]["results"])).load_state(g["sources"][0]["run"])["qa_snapshot"]
    )
    snapshot["rows"] = [r for r in snapshot["rows"] if r["qa_id"] not in all_present]
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    scoring = old_folder / "judge"
    previous = json.loads((scoring / "preserved-scores.json").read_text())
    values = {q: v["result"] for q, v in previous.items()}
    diags = {}
    for v in previous.values():
        s = json.loads(Path(v["path"]).read_text())
        r = v["result"]
        diags[r["diagnostic_key"]] = s["diagnostic_results"][r["diagnostic_key"]]
    for p in sorted(scoring.glob("batch-*/scoring-state.json")):
        s = json.loads(p.read_text())
        for r in s.get("scoring_results", {}).values():
            d = s.get("diagnostic_results", {}).get(r.get("diagnostic_key"), {})
            if r.get("status") == "completed" and d.get("status") == "completed":
                values[r["qa_id"]] = r
                diags[r["diagnostic_key"]] = d
    save(ROOT / "preserved-scores.json", {"scoring_results": values, "diagnostic_results": diags})
    prepared = ROOT / "prepared"
    prepared.mkdir()
    folder = prepared / "improved-limited"
    folder.mkdir()
    save(folder / "snapshot.json", snapshot)
    g.update(
        concurrency=16,
        sources=sources,
        inherited=len(all_present),
        remaining=len(snapshot["rows"]),
        previous_score_paths=[str(ROOT / "preserved-scores.json")],
    )
    save(prepared / "intent.json", {"groups": [g], "at": time.time()})
    env["PYTHONPATH"] = g["evaluation_source"] + ":" + env.get("PYTHONPATH", "")
    log = (ROOT / "worker.log").open("x")
    child = subprocess.Popen(
        [sys.executable, str(ROOT / "continue_four.py"), str(prepared), "--worker", "improved-limited"],
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    save(
        ROOT / "launched.json",
        {
            "pid": child.pid,
            "inherited": len(all_present),
            "remaining": len(snapshot["rows"]),
            "reused_scores": len(values),
            "salvaged": len(recovered),
            "concurrency": 16,
            "at": time.time(),
        },
    )
    print(
        json.dumps({"qa_pid": child.pid, "inherited": len(all_present), "remaining": len(snapshot["rows"])})
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    try:
        apply()
    except Exception as exc:
        save(ROOT / "failure.json", {"error": str(exc), "type": type(exc).__name__, "at": time.time()})
        raise
