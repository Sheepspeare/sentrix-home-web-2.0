"""Exercise recovery preparation against the real SQLite job repository."""

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import recover_videos as recovery


def test_prepare_freezes_plan_before_queue_and_reuses_job(tmp_path, monkeypatch):
    import sentrix.config

    source = tmp_path / "source.mov"
    source.write_bytes(b"original-video")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset_id = "asset_fixture"
    analysis = tmp_path / "media/derived/svd_katna" / asset_id / "analysis.json"
    analysis.parent.mkdir(parents=True)
    analysis.write_text('{"frozen":true}')
    windows = [{"index": 0, "start_sec": 0.0, "end_sec": 1.0, "selected_timestamp_sec": 0.999999}]
    row = {
        "asset_id": asset_id,
        "source_sha256": digest,
        "analysis_sha256": recovery.sha(analysis),
        "existing_derived": [],
        "planned_windows": windows,
    }
    migrations = Path(sentrix.config.__file__).parent / "infrastructure/persistence/migrations"
    with sqlite3.connect(tmp_path / "sentrix.db") as connection:
        for name in ("0002_media.sql", "0025_video_jobs.sql", "0026_video_job_config_hash.sql"):
            connection.executescript((migrations / name).read_text())
        connection.execute(
            "INSERT INTO assets(id,scope_id,file_name,media_type,path,content_sha256,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (asset_id, recovery.SCOPE, "source.mov", "video", str(source), digest, "a", "a"),
        )
    monkeypatch.setattr(recovery, "DATA", tmp_path)
    monkeypatch.setattr(recovery, "process_env", lambda _: {})
    settings = SimpleNamespace(
        data_dir=tmp_path,
        embedding_scheme="wemm",
        cross_modal_attention_enabled=True,
        qwen_asr_model="existing",
        vlm_base_url="existing",
        legacy_clip_url="existing",
    )
    monkeypatch.setattr(sentrix.config, "Settings", lambda **_: settings)
    job = recovery.prepare(row)
    assert job["status"] == "QUEUED"
    plan = tmp_path / "video-jobs" / job["job_id"] / "scene_windows.json"
    assert json.loads(plan.read_text()) == windows
    with sqlite3.connect(tmp_path / "sentrix.db") as connection:
        stage = connection.execute(
            "SELECT input_hash,status FROM video_job_stages WHERE job_id=? AND stage='FRAME_SAMPLE_VSD'",
            (job["job_id"],),
        ).fetchone()
        assert stage[0] == recovery.sha(plan)
        assert stage[1] == "PENDING"
        assert connection.execute("SELECT count(*) FROM video_jobs").fetchone()[0] == 1
    assert recovery.prepare(row)["job_id"] == job["job_id"]
    assert recovery.sha(analysis) == row["analysis_sha256"]


def test_coverage_rejects_frame_without_vector_indexes():
    row = {"planned_windows": [{"index": 0, "start_sec": 0.0, "end_sec": 1.0, "selected_timestamp_sec": 0.0}]}
    frame = {
        "source_scene_index": 0,
        "source_timestamp_sec": 0.0,
        "observations": 1,
        "visual_vectors": 0,
        "text_vectors": 0,
    }
    scene = {"source_scene_index": 0, "source_start_sec": 0.0, "source_end_sec": 1.0}
    with pytest.raises(AssertionError):
        recovery.assert_complete({"asset_id": "asset_fixture", "frames": [frame], "scenes": [scene]}, row)


def test_model_health_ignores_original_errors_and_reports_new_failure(tmp_path):
    log = tmp_path / "requests.jsonl"
    records = [
        {"id": "old-error", "started_at": 1, "http_status": 400},
        {
            "id": "new-success",
            "started_at": 11,
            "http_status": 200,
            "actual_model": "qwen3.8-flash",
            "enable_thinking": False,
        },
        {"id": "new-error", "started_at": 12, "http_status": 502},
    ]
    log.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    assert [row["id"] for row in recovery.model_failures_since(10, log)] == ["new-error"]
