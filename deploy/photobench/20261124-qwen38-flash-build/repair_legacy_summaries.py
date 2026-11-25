"""Freeze and finish old video scene placeholders using the existing summary path."""

import argparse
import json
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import repair_content as repair
from recover_videos import DATA, ROOT, SCOPE, connect, save, sha
from resume_content import execute

DIRECTORY = ROOT / "content/legacy-scene-summaries"


def select_targets(events, excluded):
    targets = [
        event
        for event in events
        if event["scope_id"] == SCOPE
        and event["source_type"] == "video_scene"
        and event["summary"].startswith("视频场景 · ")
    ]
    assert not ({event["id"] for event in targets} & set(excluded))
    assert len(targets) == len({event["id"] for event in targets})
    return targets


def freeze():
    repair.assert_idle()
    assert not DIRECTORY.exists(), "Read the existing frozen plan before continuing"
    original = json.loads((repair.CONTENT / "plan.json").read_text())
    with connect() as connection:
        events = [
            dict(r) for r in connection.execute("SELECT * FROM events WHERE scope_id=? ORDER BY id", (SCOPE,))
        ]
        targets = select_targets(events, [e["id"] for e in original["scene_events"]])
        assert len(targets) == 1328
        observations = {}
        for event in targets:
            observations[event["id"]] = [
                dict(r)
                for r in connection.execute(
                    "SELECT o.* FROM observations o JOIN event_observations eo ON eo.observation_id=o.id "
                    "WHERE eo.event_id=? ORDER BY o.id",
                    (event["id"],),
                )
            ]
        eligible = [
            e
            for e in targets
            if observations[e["id"]] and all(o["caption"].strip() for o in observations[e["id"]])
        ]
        assert len(eligible) >= 4
        ranked = sorted(eligible, key=lambda e: sum(len(o["caption"]) for o in observations[e["id"]]))
        smoke = [ranked[i]["id"] for i in (0, len(ranked) // 3, 2 * len(ranked) // 3, len(ranked) - 1)]
        assert len(set(smoke)) == 4
        assert shutil.disk_usage(DIRECTORY.parent).free > (DATA / "sentrix.db").stat().st_size + 512 * 1024**2
        DIRECTORY.mkdir()
        with sqlite3.connect(DIRECTORY / "before-legacy.db") as backup:
            connection.backup(backup)
    plan = {
        "scope_id": SCOPE,
        "created_at": time.time(),
        "scene_events": targets,
        "observations": observations,
        "smoke_ids": smoke,
        "original_plan_sha256": sha(repair.CONTENT / "plan.json"),
        "excluded_successful_new_scene_count": len(original["scene_events"]),
        "missing_caption_ids": [e["id"] for e in targets if e not in eligible],
        "model": repair.MODEL,
        "enable_thinking": False,
        "max_tokens": 4096,
        "workers": 4,
    }
    save(DIRECTORY / "plan.json", plan)
    print(
        json.dumps(
            {"planned": len(targets), "smoke_ids": smoke, "missing_caption_ids": plan["missing_caption_ids"]}
        )
    )


def guarded_execute(before, observations, repos):
    for expected in observations:
        assert repair.snapshot("observations", expected["id"]) == expected, "Frozen input changed"
    directory = repair.CONTENT / "summary" / before["id"]
    if (
        not (directory / "success.json").exists()
        and not (directory / "model/accepted-response.json").exists()
    ):
        assert repair.snapshot("events", before["id"]) == before, "Frozen event changed"
    return execute("summary", before, repos)


def run(output, smoke=False):
    repair.assert_idle()
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    plan_path = DIRECTORY / "plan.json"
    plan = json.loads(plan_path.read_text())
    assert plan["scope_id"] == SCOPE and len(plan["scene_events"]) == 1328
    assert sha(repair.CONTENT / "plan.json") == plan["original_plan_sha256"]
    assert not output.exists(), "Inspect the terminal and saved successes before continuing"
    output.mkdir(parents=True)
    repair.CONTENT = DIRECTORY
    repos = repair.repositories()
    targets = [e for e in plan["scene_events"] if not smoke or e["id"] in plan["smoke_ids"]]
    assert len(targets) == (4 if smoke else 1328)
    progress = {
        "status": "running",
        "started_at": time.time(),
        "plan_sha256": sha(plan_path),
        "mode": "smoke" if smoke else "full",
        "planned": len(targets),
        "results": [],
    }
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(guarded_execute, e, plan["observations"][e["id"]], repos) for e in targets]
            try:
                for future in as_completed(futures):
                    progress["results"].append(future.result())
                    progress["updated_at"] = time.time()
                    save(output / "progress.json", progress)
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        progress["unresolved_count"] = sum(
            r["status"] not in {"completed", "reused"} for r in progress["results"]
        )
        progress["status"] = "completed_with_gaps" if progress["unresolved_count"] else "completed"
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(output / "terminal.json", progress)
        print(json.dumps({k: v for k, v in progress.items() if k != "results"}))
    return 1 if progress["unresolved_count"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("freeze", "smoke", "run"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        freeze()
    else:
        assert args.output is not None
        raise SystemExit(run(args.output, smoke=args.action == "smoke"))
