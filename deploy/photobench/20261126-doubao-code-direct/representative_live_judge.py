"""Continuously score saved representative answers, owning the previous final scoring directory."""

import argparse
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

from representative_qa import load, process_environment, save


def main(root, name, recovery=False):
    plan = json.loads((root / "intent.json").read_text())
    previous_root = Path(plan["score_root"])
    previous = json.loads((previous_root / "intent.json").read_text())
    folder = root / name
    scoring = folder / ("judge-recovery-1" if recovery else "judge")
    # The original QA worker uses mkdir without exist_ok after finishing QA.
    # Reserving this directory transfers final scoring ownership without stopping QA.
    scoring.mkdir()
    save(
        scoring / "ownership.json",
        {
            "owner": "representative_live_judge",
            "pid": os.getpid(),
            "at": time.time(),
            "original_final_mkdir": "expected handoff stop after QA",
        },
    )
    (scoring / "intent.json").write_bytes((previous_root / "intent.json").read_bytes())
    (scoring / "protocol.json").write_bytes((previous_root / "protocol.json").read_bytes())
    from photobench.infrastructure.results.store import FileRunStore

    store = FileRunStore(folder / "results")
    run = json.loads((folder / "started.json").read_text())["run_id"]
    owner = load("live_follow", previous["follow_source"]).Follow(scoring)
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
    load("live_transport", previous["transport_source"]).install()
    from photobench.application.run_execution.judge import JudgeRunner

    runner = JudgeRunner(
        http=owner.http(name),
        judge_url=owner.protocol["url"],
        judge_model=owner.protocol["model"],
        judge_api_key=key,
        cancel=threading.Event(),
        evaluation_options=owner.protocol["evaluation_options"],
    )
    scored = {}
    if recovery:
        for old in sorted((folder / "judge").glob("batch-*/scoring-state.json")):
            state = json.loads(old.read_text())
            for value in state.get("scoring_results", {}).values():
                if value.get("status") == "completed":
                    diagnostic = state["diagnostic_results"][value["diagnostic_key"]]
                    assert diagnostic["status"] == "completed"
                    scored[value["qa_id"]] = value
        save(scoring / "preservation.json", {"reused_valid": len(scored)})
    batch = 0
    save(scoring / "started.json", {"at": time.time(), "run_id": run, "concurrency": 2, "direct_cloud": True})
    while True:
        state = store.load_state(run)
        items = [
            v for v in state["items"] if v.get("execution_status") == "completed" and v["qa_id"] not in scored
        ]
        if items:
            batch += 1
            out = scoring / f"batch-{batch:04d}"
            out.mkdir()
            source = {"run_id": run, "qa_snapshot": state["qa_snapshot"], "items": items}
            save(out / "input.json", source)
            result = owner.scorer.score_saved(source, owner.protocol, out, runner, concurrency=2)
            saved = json.loads((out / "scoring-state.json").read_text())
            for value in saved.get("scoring_results", {}).values():
                if value.get("status") == "completed":
                    scored[value["qa_id"]] = value
            save(
                scoring / "progress.json",
                {
                    "at": time.time(),
                    "valid": len(scored),
                    "mean": sum(v["normalized_score"] for v in scored.values()) / len(scored)
                    if scored
                    else None,
                    "qa_processed": len(state["items"]),
                    "qa_total": len(state["qa_snapshot"]["rows"]),
                    "batch": batch,
                    "last_result": result,
                },
            )
            if result["status"] != "completed":
                save(
                    scoring / "terminal.json",
                    {"status": "needs_review", "result": result, "valid": len(scored)},
                )
                return 1
        if state.get("finished_at"):
            save(
                scoring / "terminal.json",
                {
                    "status": "completed",
                    "valid": len(scored),
                    "qa_processed": len(state["items"]),
                    "mean": sum(v["normalized_score"] for v in scored.values()) / len(scored)
                    if scored
                    else None,
                },
            )
            return 0
        time.sleep(15)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("group")
    p.add_argument("--launch", action="store_true")
    p.add_argument("--recovery", action="store_true")
    a = p.parse_args()
    if a.launch:
        import subprocess

        plan = json.loads((a.root / "intent.json").read_text())
        g = next(g for g in plan["groups"] if g["name"] == a.group)
        _, _, env = process_environment(g["port"])
        env["PYTHONPATH"] = g["evaluation_source"] + ":" + env.get("PYTHONPATH", "")
        log = (a.root / (a.group + ("-live-judge-recovery-1.log" if a.recovery else "-live-judge.log"))).open(
            "x"
        )
        child = subprocess.Popen(
            [sys.executable, __file__, str(a.root), a.group] + (["--recovery"] if a.recovery else []),
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        save(a.root / (a.group + "-live-judge-launch.json"), {"pid": child.pid, "at": time.time()})
        print(json.dumps({"group": a.group, "pid": child.pid}))
    else:
        try:
            raise SystemExit(main(a.root, a.group, a.recovery))
        except Exception as e:
            save(a.root / a.group / "live-judge-failure.json", {"type": type(e).__name__, "error": str(e)})
            raise
