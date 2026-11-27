"""Preserve completed turns, score saved answers, then dispatch only unstarted QA."""

import copy
import json
import sqlite3
import time
from pathlib import Path

from representative_qa import get, process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
OLD = B / "baseline-account27-recovery1-20261127"
ROOT = B / "baseline-account27-recovery2-20261127"


def main():
    import account_retry

    from photobench.application.run_execution.service import RunRequest, index_assets_by_name
    from photobench.bootstrap.container import Container
    from photobench.config import Settings
    from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
    from photobench.domain.runtime.ports import RunRuntimeSnapshot
    from photobench.infrastructure.results.store import FileResultWriter, FileRunStore

    assert not ROOT.exists()
    plan = json.loads((OLD / "intent.json").read_text())
    g = plan["group"]
    started = json.loads((OLD / "started.json").read_text())
    state = FileRunStore(OLD / "results").load_state(started["run_id"])
    _, _, env = process_environment(8886)
    _, _, product = process_environment(8306)
    db = sqlite3.connect((Path(product["SENTRIX_DATA_DIR"]) / "sentrix.db").as_uri() + "?mode=ro", uri=True)
    assert (
        db.execute("select count(*) from agent_turns where status in ('running','pending')").fetchone()[0]
        == 0
    )
    cut = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started["at"])) + "+00:00"
    rows = db.execute(
        "select c.title,t.id,t.conversation_id from agent_turns t "
        "join agent_conversations c on c.id=t.conversation_id "
        "where julianday(t.created_at)>=julianday(?)",
        (cut,),
    ).fetchall()
    present = {r["qa_id"] for r in state["items"]}
    mapping = {}
    for q, turn, conv in rows:
        if q in set(g["missing_ids"]) - present:
            assert q not in mapping
            mapping[q] = (turn, conv)
    ROOT.mkdir()
    save(
        ROOT / "intent.json", {"source": str(OLD), "existing_saved": len(present), "existing_turns": mapping}
    )
    live = get(8886, f"runs/{g['source_run']}?include_history=false")
    settings = Settings(
        config_root=Path(env["PHOTOBENCH_CONFIG_ROOT"]),
        results_root=ROOT / "projection",
        data_root=Path(env["PHOTOBENCH_DATA_ROOT"]),
        sentrix_repo_root=Path(g["evaluation_source"]).parents[2],
        sentrix_api_url="http://127.0.0.1:8306",
        qa_concurrency=2,
        sentrix_service_unit=env["PHOTOBENCH_SENTRIX_SERVICE_UNIT"],
        sentrix_start_command=env["PHOTOBENCH_SENTRIX_START_COMMAND"],
        sentrix_pid_file=Path(env["PHOTOBENCH_SENTRIX_PID_FILE"]),
    )
    c = Container.build(settings)
    req = RunRequest(
        album_id=live["album_id"],
        qa_set=live["qa_set"],
        model_profile=live["model_profile"],
        sentrix_url="http://127.0.0.1:8306",
        model_base_url=c.config.public_runtime_connection_config()["model_base_url"],
        run_id="preserved-account27",
        mode="reuse",
        scope_id=live["scope_id"],
        reasoning=live["reasoning"],
        graph_enabled=live["graph_enabled"],
        defer_scoring=True,
        runtime_snapshot=RunRuntimeSnapshot(
            json.dumps(c.config.public_runtime_connection_config()), c.build_revision
        ),
    )
    execution = c.run_execution(req)
    assets = index_assets_by_name(execution.client.list_assets(scope_id=live["scope_id"], limit=None))
    definitions = {r["qa_id"]: r for r in state["qa_snapshot"]["rows"]}
    for q, (turn, conv) in mapping.items():
        polled = execution.client.get_turn(turn_id=turn, timeout=60)
        assert polled["status"] not in ("running", "pending")
        execution.client.start_conversation = lambda conv=conv, **kw: {"id": conv}
        execution.client.start_turn = lambda turn=turn, **kw: {"turn_id": turn}
        execution._await_turn = lambda *a, polled=polled, **kw: polled
        item = execution._evaluate_one(definitions[q], live["scope_id"], assets, req)
        state["items"].append(item)
    assert len({r["qa_id"] for r in state["items"]}) == len(state["items"])
    state["run_id"] = "preserved-account27"
    FileResultWriter(ROOT / "preserved-results").write_snapshot(state["run_id"], state)
    present = {r["qa_id"] for r in state["items"]}
    snapshot = copy.deepcopy(state["qa_snapshot"])
    snapshot["rows"] = [r for r in snapshot["rows"] if r["qa_id"] not in present]
    snapshot["definition_sha256"] = qa_fingerprint(snapshot["rows"])
    missing = ROOT / "missing9"
    missing.mkdir()
    plan["group"]["missing_ids"] = [r["qa_id"] for r in snapshot["rows"]]
    plan["group"]["qa_count"] = len(snapshot["rows"])
    save(missing / "intent.json", plan)
    save(missing / "snapshot.json", snapshot)
    score_root = ROOT / "preserved-scoring"
    score_root.mkdir()
    source = {
        "run_id": state["run_id"],
        "qa_snapshot": state["qa_snapshot"],
        "items": [v for v in state["items"] if v.get("execution_status") == "completed"],
    }
    save(
        ROOT / "prepared.json",
        {
            "preserved": len(present),
            "missing": len(snapshot["rows"]),
            "scoreable_preserved": len(source["items"]),
            "qa_requests_during_recovery": 0,
        },
    )
    # Start only the genuinely unstarted UUIDs. The existing eighteen are excluded.
    account_retry.launch(missing)
    account_retry.score_existing(score_root, source, len(present), Path(plan["score_root"]))
    print(json.dumps({"preserved": len(present), "missing": len(snapshot["rows"])}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if ROOT.exists():
            save(ROOT / "failure.json", {"error": str(exc), "type": type(exc).__name__})
        raise
