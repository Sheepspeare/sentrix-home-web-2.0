"""Recover existing turns and continue frozen full evaluations after disk repair."""

import argparse
import copy
import json
import sqlite3
import time
from pathlib import Path

from representative_qa import get, process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
ROOT = B / "full-disk-recovery-20261127"
SPECS = {
    "baseline-large": (B / "full-four-continue-20261127/prepared", None),
    "improved-large": (B / "full-four-continue-20261127/prepared", None),
    "improved-limited": (
        B / "improved-limited-continue16-20261127/prepared",
        B / "improved-limited-judge-recovery1-20261127/improved-limited/judge",
    ),
    "planner-large": (
        B / "planner-large-continue16-20261127/prepared",
        B / "planner-large-judge-recovery1-20261127/planner-large/judge",
    ),
}


def prepare(name):
    from photobench.application.run_execution.service import RunRequest, index_assets_by_name
    from photobench.bootstrap.container import Container
    from photobench.config import Settings
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
    from photobench.domain.runtime.ports import RunRuntimeSnapshot
    from photobench.infrastructure.results.store import FileResultWriter, FileRunStore

    old_root, recovery_scoring = SPECS[name]
    old = old_root / name
    g = json.loads((old / "started.json").read_text())
    proc = Path(f"/proc/{g['pid']}/stat")
    assert not proc.exists() or proc.read_text().split(") ")[1].split()[0] == "Z"
    folder = ROOT / "prepared" / name
    folder.mkdir(parents=True)
    assert not (folder / "recovery-intent.json").exists()
    save(folder / "recovery-intent.json", {"old": str(old), "old_run": g["run_id"], "at": time.time()})
    _, _, env = process_environment(g["port"])
    _, _, product_env = process_environment(g["product"])
    conf = get(g["product"], "diagnostics")["runtime_configuration"]
    assert conf["agent_turn_workers"] == 16
    assert conf["vlm_context_tokens"] == conf["vlm_reasoning_context_tokens"] == 131072
    assert conf["reasoning_max_model_calls"] == (20 if name.endswith("limited") else 100)
    assert conf["reasoning_max_tool_calls"] == (30 if name.endswith("limited") else 600)
    state = FileRunStore(old / "results").load_state(g["run_id"])
    present = {v["qa_id"] for v in state["items"]}
    definitions = {r["qa_id"]: r for r in state["qa_snapshot"]["rows"]}
    db = sqlite3.connect(
        (Path(product_env["SENTRIX_DATA_DIR"]) / "sentrix.db").as_uri() + "?mode=ro", uri=True
    )
    cut = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(g["at"])) + "+00:00"
    turns = db.execute(
        "select c.title,t.id,t.conversation_id from agent_turns t "
        "join agent_conversations c on c.id=t.conversation_id "
        "where julianday(t.created_at)>=julianday(?)",
        (cut,),
    ).fetchall()
    mapping = {}
    for q, turn, conv in turns:
        if q in definitions and q not in present:
            assert q not in mapping, (name, q, "ambiguous existing turns")
            mapping[q] = (turn, conv)
    save(folder / "existing-turns.json", mapping)
    live = get(g["port"], f"runs/{g['source_run']}?include_history=false")
    settings = Settings(
        config_root=Path(env["PHOTOBENCH_CONFIG_ROOT"]),
        results_root=folder / "projection",
        data_root=Path(env["PHOTOBENCH_DATA_ROOT"]),
        sentrix_repo_root=Path(g["evaluation_source"]).parents[2],
        sentrix_api_url=f"http://127.0.0.1:{g['product']}",
        qa_concurrency=16,
        sentrix_service_unit=env["PHOTOBENCH_SENTRIX_SERVICE_UNIT"],
        sentrix_start_command=env["PHOTOBENCH_SENTRIX_START_COMMAND"],
        sentrix_pid_file=Path(env["PHOTOBENCH_SENTRIX_PID_FILE"]),
    )
    c = Container.build(settings)
    connection = c.config.public_runtime_connection_config()
    req = RunRequest(
        album_id=live["album_id"],
        qa_set=live["qa_set"],
        model_profile=live["model_profile"],
        sentrix_url=f"http://127.0.0.1:{g['product']}",
        model_base_url=connection["model_base_url"],
        run_id=f"preserved-{name}",
        mode="reuse",
        scope_id=live["scope_id"],
        reasoning=live["reasoning"],
        graph_enabled=live["graph_enabled"],
        defer_scoring=True,
        runtime_snapshot=RunRuntimeSnapshot(json.dumps(connection), c.build_revision),
    )
    ex = c.run_execution(req)
    assets = index_assets_by_name(ex.client.list_assets(scope_id=live["scope_id"], limit=None))
    recovered = []
    for q, (turn, conv) in mapping.items():
        while True:
            polled = ex.client.get_turn(turn_id=turn, timeout=60)
            if polled["status"] not in ("pending", "running"):
                break
            save(folder / "recovery-progress.json", {"status": "await_existing", "qa_id": q, "turn_id": turn})
            time.sleep(10)
        ex.client.start_conversation = lambda conv=conv, **kw: {"id": conv}
        ex.client.start_turn = lambda turn=turn, **kw: {"turn_id": turn}
        ex._await_turn = lambda *a, polled=polled, **kw: polled
        item = ex._evaluate_one(definitions[q], live["scope_id"], assets, req)
        recovered.append(item)
    state["items"].extend(recovered)
    assert len(state["items"]) == len({v["qa_id"] for v in state["items"]})
    state["run_id"] = f"preserved-{name}"
    FileResultWriter(folder / "preserved-results").write_snapshot(state["run_id"], state)
    sources = g["sources"] + [
        {"results": str(folder / "preserved-results"), "run": state["run_id"], "count": len(state["items"])}
    ]
    all_present = set()
    snapshot = None
    for src in sources:
        source = FileRunStore(Path(src["results"])).load_state(src["run"])
        if snapshot is None:
            snapshot = copy.deepcopy(source["qa_snapshot"])
        all_present.update(v["qa_id"] for v in source["items"])
    assert len(snapshot["rows"]) == 1013
    snapshot["rows"] = [r for r in snapshot["rows"] if r["qa_id"] not in all_present]
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    values, diagnostics = {}, {}
    for score_dir in [old / "judge", recovery_scoring]:
        if score_dir is None or not score_dir.exists():
            continue
        preserved = score_dir / "preserved-scores.json"
        if preserved.exists():
            for v in json.loads(preserved.read_text()).values():
                result = v["result"]
                saved = json.loads(Path(v["path"]).read_text())
                values[result["qa_id"]] = result
                diagnostics[result["diagnostic_key"]] = saved["diagnostic_results"][result["diagnostic_key"]]
        for path in sorted(score_dir.glob("batch-*/scoring-state.json")):
            saved = json.loads(path.read_text())
            for result in saved.get("scoring_results", {}).values():
                diag = saved.get("diagnostic_results", {}).get(result.get("diagnostic_key"), {})
                if result.get("status") == "completed" and diag.get("status") == "completed":
                    values[result["qa_id"]] = result
                    diagnostics[result["diagnostic_key"]] = diag
    save(folder / "preserved-scores.json", {"scoring_results": values, "diagnostic_results": diagnostics})
    g.update(
        concurrency=16,
        inherited=len(all_present),
        remaining=len(snapshot["rows"]),
        sources=sources,
        previous_score_paths=[str(folder / "preserved-scores.json")],
        allow_shared_product=True,
        read_legacy_snapshots=True,
        authoritative_scores=str(folder / "preserved-scores.json"),
    )
    assert g["inherited"] + g["remaining"] == 1013
    save(folder / "snapshot.json", snapshot)
    save(folder / "inherited-manifest.json", {"qa_ids": sorted(all_present), "sources": sources})
    save(
        folder / "prepared.json",
        {
            "group": g,
            "recovered_existing_turns": len(recovered),
            "reused_scores": len(values),
            "at": time.time(),
        },
    )
    print(
        json.dumps(
            {
                "name": name,
                "inherited": len(all_present),
                "remaining": len(snapshot["rows"]),
                "recovered": len(recovered),
                "scores": len(values),
            }
        ),
        flush=True,
    )


def launch():
    groups = [json.loads((ROOT / "prepared" / n / "prepared.json").read_text())["group"] for n in SPECS]
    assert not (ROOT / "prepared/intent.json").exists()
    save(ROOT / "prepared/intent.json", {"groups": groups, "judge_concurrency_per_group": 2})
    import continue_four

    continue_four.launch(ROOT / "prepared")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", choices=list(SPECS))
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare)
    else:
        launch()
