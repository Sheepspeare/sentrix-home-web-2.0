"""Resume the frozen failed-video set through the existing product process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from operate import BASE, DATA, PRODUCT_UNIT, assert_idle, call, process_env, processes
from probe_finalization import SCOPE, inspect

ROOT = BASE / "repair-ee6594ae"
RUN = "20261124-221506-2026.11.17-atm-sources-addresses-qwen3.8-flash-9b48ee"


def sha(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def connect():
    connection = sqlite3.connect(f"{(DATA / 'sentrix.db').as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def plans(path):
    value = json.loads(path.read_text())
    assert value["scope_id"] == SCOPE
    rows = value["results"]
    assert len(rows) == len({row["asset_id"] for row in rows}) == 259
    assert sum(len(row["planned_windows"]) for row in rows) == 955
    return rows


def coverage(connection, asset_id):
    asset = connection.execute("SELECT * FROM assets WHERE id=? AND scope_id=?", (asset_id, SCOPE)).fetchone()
    assert asset is not None
    frames = [
        dict(row)
        for row in connection.execute(
            "SELECT a.id,a.status,a.path,a.content_sha256,a.source_scene_index,"
            "a.source_timestamp_sec,"
            "(SELECT count(*) FROM observations o WHERE o.asset_id=a.id) AS observations,"
            "(SELECT count(*) FROM memory_vectors v WHERE v.source_id=a.id "
            "AND v.space='visual') AS visual_vectors,"
            "(SELECT count(*) FROM text_vectors t JOIN observations o ON t.source_id=o.id "
            "WHERE o.asset_id=a.id) AS text_vectors "
            "FROM assets a WHERE a.parent_asset_id=? ORDER BY a.source_scene_index",
            (asset_id,),
        )
    ]
    scenes = [
        dict(row)
        for row in connection.execute(
            "SELECT id,source_scene_index,source_start_sec,source_end_sec,summary,revision "
            "FROM events WHERE source_asset_id=? AND source_type='video_scene' ORDER BY source_scene_index",
            (asset_id,),
        )
    ]
    return {"asset_id": asset_id, "status": asset["status"], "frames": frames, "scenes": scenes}


def validate_preserved(connection, row):
    source = connection.execute("SELECT * FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
    assert source and source["scope_id"] == SCOPE and source["media_type"] == "video"
    assert sha(source["path"]) == row["source_sha256"] == source["content_sha256"]
    analysis = DATA / "media/derived/svd_katna" / row["asset_id"] / "analysis.json"
    assert sha(analysis) == row["analysis_sha256"]
    for prior in row["existing_derived"]:
        frame = connection.execute("SELECT * FROM assets WHERE id=?", (prior["id"],)).fetchone()
        assert frame and frame["parent_asset_id"] == row["asset_id"]
        assert frame["path"] == prior["path"]
        assert frame["source_timestamp_sec"] == prior["source_timestamp_sec"]
        assert sha(frame["path"]) == prior["content_sha256"] == frame["content_sha256"]
    return source


def assert_complete(current, row):
    expected = row["planned_windows"]
    assert len(current["scenes"]) == len(current["frames"]) == len(expected), current["asset_id"]
    for frame, scene, window in zip(current["frames"], current["scenes"], expected, strict=True):
        assert frame["source_scene_index"] == scene["source_scene_index"] == window["index"]
        assert frame["source_timestamp_sec"] == window["selected_timestamp_sec"]
        assert scene["source_start_sec"] == window["start_sec"]
        assert scene["source_end_sec"] == window["end_sec"]
        assert frame["observations"] > 0 and frame["visual_vectors"] > 0 and frame["text_vectors"] > 0
        assert frame["status"] == "processed"
        assert sha(frame["path"]) == frame["content_sha256"]


def inventory(output, rows):
    value = inspect()
    with connect() as connection:
        value["source_coverage"] = [
            dict(row)
            for row in connection.execute(
                "SELECT a.media_type,count(*) AS total,"
                "sum(EXISTS(SELECT 1 FROM observations o WHERE o.asset_id=a.id)) AS with_observation,"
                "sum(EXISTS(SELECT 1 FROM memory_vectors v WHERE v.source_id=a.id "
                "AND v.space='visual')) AS with_visual_vector "
                "FROM assets a WHERE a.scope_id=? AND a.parent_asset_id IS NULL GROUP BY a.media_type",
                (SCOPE,),
            )
        ]
        value["source_without_observation"] = [
            dict(row)
            for row in connection.execute(
                "SELECT id,media_type,status,file_name FROM assets a WHERE scope_id=? "
                "AND parent_asset_id IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM observations o WHERE o.asset_id=a.id)",
                (SCOPE,),
            )
        ]
        value["video_coverage"] = [
            dict(row)
            for row in connection.execute(
                "SELECT a.id,a.status,(SELECT count(*) FROM events e WHERE e.source_asset_id=a.id "
                "AND source_type='video_scene') AS scenes FROM assets a "
                "WHERE scope_id=? AND media_type='video' "
                "AND parent_asset_id IS NULL",
                (SCOPE,),
            )
        ]
        value["repair_coverage"] = [coverage(connection, row["asset_id"]) for row in rows]
        value["video_jobs"] = [
            dict(row)
            for row in connection.execute(
                "SELECT j.* FROM video_jobs j JOIN assets a ON j.asset_id=a.id WHERE a.scope_id=?", (SCOPE,)
            )
        ]
        value["index_counts"] = {
            table: connection.execute(f"SELECT count(*) FROM {table} WHERE scope_id=?", (SCOPE,)).fetchone()[
                0
            ]
            for table in ("observations", "memory_vectors", "text_vectors")
        }
        value["schemas"] = {
            table: [dict(row) for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in ("observations", "observation_search_fts")
        }
    value["protected_pids"] = processes()
    value["gateway"] = call(18138, "/health")
    value["evaluation_active_work"] = call(8874, "/api/v1/active-work")
    save(output, value)
    print(
        json.dumps(
            {
                "output": str(output),
                "source_coverage": value["source_coverage"],
                "missing_source_observations": len(value["source_without_observation"]),
                "videos_without_scene": sum(not v["scenes"] for v in value["video_coverage"]),
                "index_counts": value["index_counts"],
            }
        )
    )


def prepare(row):
    # Import repositories only: a second Container would contend for embedded Qdrant.
    from sentrix.api.routes.media import _video_config_hash
    from sentrix.config import Settings
    from sentrix.infrastructure.persistence.database import Database
    from sentrix.infrastructure.persistence.repositories.video import SqliteVideoRepository

    env = process_env(PRODUCT_UNIT)
    os.environ.update({key: value for key, value in env.items() if key.startswith("SENTRIX_")})
    settings = Settings(_env_file=None)
    assert settings.data_dir.resolve() == DATA.resolve()
    repository = SqliteVideoRepository(Database(DATA / "sentrix.db", Path("/unused-migrations")))
    with connect() as connection:
        source = validate_preserved(connection, row)
        source = dict(source)
    job = repository.get_job_for_asset(row["asset_id"])
    if job:
        assert str(job["job_id"]).startswith("vjob_repair38_"), "Unexpected existing job; inspect first"
        return job
    job_id = "vjob_repair38_" + row["asset_id"].removeprefix("asset_")
    plan_path = DATA / "video-jobs" / job_id / "scene_windows.json"
    save(plan_path, row["planned_windows"])
    job = repository.create_job(
        row["asset_id"],
        source["content_sha256"],
        job_id,
        _video_config_hash(SimpleNamespace(settings=settings)),
    )
    repository.update_stage(
        job_id,
        "UPLOAD",
        status="SUCCEEDED",
        progress=1.0,
        input_hash=source["content_sha256"],
        output_artifact=source["path"],
    )
    repository.update_stage(
        job_id, "FRAME_SAMPLE_VSD", input_hash=sha(plan_path), output_artifact=str(plan_path)
    )
    return job


def run(rows, output, limit):
    assert_idle()
    assert call(18138, "/health")["active_requests"] == 0
    with connect() as connection:
        receipt = connection.execute(
            "SELECT status FROM pipeline_receipts WHERE operation_id=?", ("f0aaa1a1da8544b692b22fb1bf4d0ef3",)
        ).fetchone()
        assert receipt and receipt["status"] == "completed", dict(receipt) if receipt else None
    selected = rows[:limit] if limit else rows
    records = []
    started = time.time()
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / "terminal.json").exists(), "Preserve prior execution; choose another output"
    try:
        for row in selected:
            with connect() as connection:
                validate_preserved(connection, row)
                before = coverage(connection, row["asset_id"])
            if len(before["scenes"]) == len(row["planned_windows"]):
                assert_complete(before, row)
                records.append({"asset_id": row["asset_id"], "status": "already_complete"})
                save(output / "progress.json", {"started_at": started, "results": records})
                continue
            job = prepare(row)
            job_id = str(job["job_id"])
            save(output / f"{job_id}-before.json", {"job": job, "coverage": before})
            assert job["status"] in {"QUEUED", "RETRY_WAIT"}, job
            # Save intent first. An ambiguous HTTP result is resolved by GET, never by a second POST.
            save(output / f"{job_id}-intent.json", {"at": time.time(), "job_id": job_id})
            try:
                response = call(8294, f"/api/v1/video-jobs/{job_id}/resume", {}, timeout=60)
                save(output / f"{job_id}-accepted.json", response)
            except Exception as error:
                save(output / f"{job_id}-submission-error.json", {"error": str(error), "at": time.time()})
            signature, changed, errors = None, time.monotonic(), 0
            while True:
                time.sleep(10)
                try:
                    current = call(8294, f"/api/v1/video-jobs/{job_id}", timeout=60)
                    errors = 0
                except Exception:
                    errors += 1
                    if errors >= 5:
                        raise
                    continue
                pulse = json.dumps(
                    [current["status"], current["current_stage"], current["stages"]], sort_keys=True
                )
                if pulse != signature:
                    signature, changed = pulse, time.monotonic()
                save(
                    output / "progress.json",
                    {
                        "at": time.time(),
                        "started_at": started,
                        "requested": len(selected),
                        "completed": len(records),
                        "results": records,
                        "current": current,
                        "no_progress_seconds": time.monotonic() - changed,
                    },
                )
                if current["status"] in {"COMPLETED", "DEGRADED", "FAILED", "CANCELLED"}:
                    break
                if time.monotonic() - changed > 1800:
                    raise RuntimeError(f"No stage progress for 30 minutes: {job_id}; inspect the running job")
            save(output / f"{job_id}-terminal.json", current)
            assert current["status"] == "COMPLETED", current
            with connect() as connection:
                validate_preserved(connection, row)
                after = coverage(connection, row["asset_id"])
            save(output / f"{job_id}-after.json", after)
            assert_complete(after, row)
            records.append(
                {
                    "asset_id": row["asset_id"],
                    "job_id": job_id,
                    "status": "completed",
                    "frames": len(after["frames"]),
                    "scenes": len(after["scenes"]),
                }
            )
            save(
                output / "progress.json",
                {
                    "at": time.time(),
                    "started_at": started,
                    "requested": len(selected),
                    "completed": len(records),
                    "results": records,
                },
            )
        result = {
            "status": "completed",
            "requested": len(selected),
            "results": records,
            "started_at": started,
            "finished_at": time.time(),
        }
        save(output / "terminal.json", result)
        print(json.dumps({key: value for key, value in result.items() if key != "results"}))
        return 0
    except Exception as error:
        save(
            output / "terminal.json",
            {
                "status": "needs_review",
                "error_type": type(error).__name__,
                "error": str(error),
                "results": records,
                "started_at": started,
                "finished_at": time.time(),
            },
        )
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inventory", "run"])
    parser.add_argument("--plans", type=Path, default=ROOT / "recovery-plans.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    rows = plans(args.plans)
    if args.action == "inventory":
        inventory(args.output, rows)
        return 0
    return run(rows, args.output, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
