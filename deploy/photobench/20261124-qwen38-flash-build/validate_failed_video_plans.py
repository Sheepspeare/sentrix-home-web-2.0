"""Validate repair plans without changing album data or calling any model."""

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
from probe_finalization import DATABASE, SCOPE


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_video(asset_id, extractor_class):
    with sqlite3.connect(f"{DATABASE.as_uri()}?mode=ro", uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT id,path,content_sha256 FROM assets WHERE id=? AND scope_id=?", (asset_id, SCOPE)
        ).fetchone()
        if row is None:
            raise ValueError(f"asset not in expected scope: {asset_id}")
        assert (
            connection.execute("SELECT count(*) FROM events WHERE source_asset_id=?", (asset_id,)).fetchone()[
                0
            ]
            == 0
        ), asset_id
        derived = [
            dict(item)
            for item in connection.execute(
                "SELECT id,path,content_sha256,source_scene_index,source_timestamp_sec "
                "FROM assets WHERE parent_asset_id=? ORDER BY source_scene_index",
                (asset_id,),
            )
        ]
    source = Path(row["path"])
    assert digest(source) == row["content_sha256"], f"source SHA mismatch: {asset_id}"
    analysis_path = DATABASE.parent / "media/derived/svd_katna" / asset_id / "analysis.json"
    analysis = json.loads(analysis_path.read_text())
    old_times = analysis["selected_timestamps_sec"]
    sampling = analysis["sampling"]
    assert sampling.get("timestamp_basis") is None, "Expected frozen original frame-index plan"
    indices = [round(timestamp * sampling["source_fps"]) for timestamp in old_times]
    selected_indices = set(indices)
    capture = cv2.VideoCapture(str(source))
    pts = {}
    index = 0
    while True:
        ok, _ = capture.read()
        if not ok:
            break
        if index in selected_indices:
            pts[index] = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
        index += 1
    capture.release()
    assert index == sampling["decoded_frame_count"], f"decoded count changed: {asset_id}"
    assert len(pts) == len(selected_indices), f"selected frames missing: {asset_id}"
    existing = {item["source_scene_index"]: item for item in derived}
    windows = []
    with tempfile.TemporaryDirectory(prefix="sentrix-video-plan-") as temporary:
        extractor = extractor_class(Path(temporary))
        for position, (frame_index, old_timestamp) in enumerate(zip(indices, old_times, strict=True)):
            timestamp = pts[frame_index]
            prior = existing.get(position)
            if prior:
                assert digest(prior["path"]) == prior["content_sha256"], f"derived SHA mismatch: {asset_id}"
                result = "reuse_existing_file"
            else:
                output = extractor.extract(source, asset_id, position, timestamp)
                assert output.stat().st_size > 0
                result = "validated_missing_frame_in_temporary_directory"
            windows.append(
                {
                    "index": position,
                    "start_sec": 0.0 if position == 0 else (old_times[position - 1] + old_timestamp) / 2,
                    "end_sec": sampling["duration_sec"]
                    if position == len(old_times) - 1
                    else (old_timestamp + old_times[position + 1]) / 2,
                    "selected_timestamp_sec": timestamp,
                    "original_timestamp_sec": old_timestamp,
                    "decoded_frame_index": frame_index,
                    "validation": result,
                }
            )
    return {
        "asset_id": asset_id,
        "source_sha256": row["content_sha256"],
        "analysis_sha256": digest(analysis_path),
        "original_analysis_preserved": True,
        "existing_derived": derived,
        "planned_windows": windows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--extractor", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Keep prior validation results; choose a new output path"
    manifest = json.loads(args.manifest.read_text())
    assert manifest["scope_id"] == SCOPE
    ids = manifest["asset_ids"]
    assert len(ids) == len(set(ids)) == 259
    spec = importlib.util.spec_from_file_location("repaired_video_keyframes", args.extractor)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = time.time()

    def inspect(asset_id):
        try:
            return {"status": "validated", **inspect_video(asset_id, module.FfmpegVideoKeyframeExtractor)}
        except Exception as error:
            return {
                "status": "failed",
                "asset_id": asset_id,
                "error_type": type(error).__name__,
                "error": str(error),
            }

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(inspect, ids))
    failed = [item["asset_id"] for item in results if item["status"] != "validated"]
    summary = {
        "scope_id": SCOPE,
        "started_at": started,
        "finished_at": time.time(),
        "requested": len(ids),
        "validated": len(ids) - len(failed),
        "failed_ids": failed,
        "model_requests": 0,
        "album_data_modified": False,
        "extractor_sha256": digest(args.extractor),
        "results": results,
    }
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}))
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
