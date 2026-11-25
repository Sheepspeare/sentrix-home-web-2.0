"""Read-only review of a recovery alert, including content and retained records."""

import json
import time
from collections import Counter

from operate import EVAL_UNIT, GATEWAY_UNIT, PRODUCT_UNIT, call, processes, systemctl
from recover_videos import (
    BASE,
    ROOT,
    SCOPE,
    assert_complete,
    connect,
    coverage,
    plans,
    save,
    validate_preserved,
)
from verify_video_smoke import differences


def main():
    rows = plans(ROOT / "recovery-plans.json")
    previous = json.loads((ROOT / "full/terminal.json").read_text())
    alert = json.loads((ROOT / "full/model-request-alert.json").read_text())
    accepted_ids = {row["asset_id"] for row in previous["results"]}
    result = {
        "observed_at": time.time(),
        "read_only": True,
        "scope_id": SCOPE,
        "previous_terminal": {k: v for k, v in previous.items() if k != "results"},
        "alert": alert,
        "previous_completed_count": len(accepted_ids),
        "protected_pids": processes(),
        "runtime_pids": {
            unit: systemctl("show", unit, "-p", "MainPID", "--value")
            for unit in (PRODUCT_UNIT, EVAL_UNIT, GATEWAY_UNIT)
        },
        "gateway": call(18138, "/health"),
        "evaluation_work": {str(port): call(port, "/api/v1/active-work") for port in (8872, 8874)},
    }
    with connect() as connection:
        jobs = [
            dict(row)
            for row in connection.execute(
                "SELECT j.* FROM video_jobs j JOIN assets a ON j.asset_id=a.id WHERE a.scope_id=?", (SCOPE,)
            )
        ]
        result["job_status_counts"] = dict(Counter(row["status"] for row in jobs))
        result["active_jobs"] = [
            row for row in jobs if row["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "DEGRADED"}
        ]
        result["job_summaries"] = jobs
        result["complete_asset_ids"] = []
        result["unstarted_asset_ids"] = []
        result["coverage_errors"] = []
        by_asset = {row["asset_id"]: row for row in jobs}
        for row in rows:
            job = by_asset.get(row["asset_id"])
            if job is None:
                result["unstarted_asset_ids"].append(row["asset_id"])
                continue
            validate_preserved(connection, row)
            current = coverage(connection, row["asset_id"])
            if job["status"] == "COMPLETED":
                try:
                    assert_complete(current, row)
                    stages = [
                        dict(stage)
                        for stage in connection.execute(
                            "SELECT * FROM video_job_stages WHERE job_id=?", (job["job_id"],)
                        )
                    ]
                    assert stages and all(stage["status"] == "SUCCEEDED" for stage in stages)
                    result["complete_asset_ids"].append(row["asset_id"])
                except AssertionError as error:
                    result["coverage_errors"].append({"asset_id": row["asset_id"], "error": str(error)})
        frame_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT a.id AS asset_id,a.parent_asset_id,a.source_scene_index,o.* FROM observations o "
                "JOIN assets a ON o.asset_id=a.id WHERE a.parent_asset_id=? ORDER BY a.source_scene_index",
                ("asset_d16e0021b5e6",),
            )
        ]
        result["alert_video_observations"] = frame_rows
        result["alert_video_job"] = call(8294, "/api/v1/video-jobs/vjob_repair38_d16e0021b5e6")
        result["alert_video_coverage"] = coverage(connection, "asset_d16e0021b5e6")
        connection.execute(
            "ATTACH DATABASE ? AS old", ((ROOT / "before-recovery/sentrix.db").as_uri() + "?mode=ro",)
        )
        ids = tuple(row["asset_id"] for row in rows)
        placeholders = ",".join("?" for _ in ids)
        result["original_records"] = {
            "assets": differences(
                connection,
                "assets",
                f"b.id NOT IN ({placeholders}) AND coalesce(b.parent_asset_id,'') NOT IN ({placeholders})",
                ids + ids,
            )
        }
        for table in ("observations", "events", "memory_vectors", "text_vectors"):
            result["original_records"][table] = differences(connection, table)
    requests = [json.loads(line) for line in (BASE / "model-requests.jsonl").read_text().splitlines()]
    selected = [row for row in requests if row["started_at"] >= previous["started_at"]]
    result["request_counts"] = {
        "total": len(selected),
        "statuses": dict(Counter(str(row.get("http_status")) for row in selected)),
        "finishes": dict(Counter(reason for row in selected for reason in row.get("finish_reasons", []))),
    }
    result["failed_requests"] = [row for row in selected if row.get("http_status") != 200]
    result["configuration_mismatches"] = [
        row
        for row in selected
        if row.get("enable_thinking") is not False
        or (row.get("http_status") == 200 and row.get("actual_model") != "qwen3.8-flash")
    ]
    started = result["alert_video_job"]["stages"][2]["started_at"]
    from datetime import datetime

    boundary = datetime.fromisoformat(started).timestamp()
    result["alert_video_requests"] = [row for row in selected if row["started_at"] >= boundary]
    save(ROOT / "alert-8c184359-review.json", result)
    print(
        json.dumps(
            {
                "status": "review_saved",
                "jobs": result["job_status_counts"],
                "complete": len(result["complete_asset_ids"]),
                "unstarted": len(result["unstarted_asset_ids"]),
                "active": len(result["active_jobs"]),
                "coverage_errors": result["coverage_errors"],
                "requests": result["request_counts"],
                "configuration_mismatches": len(result["configuration_mismatches"]),
                "protected_changes": {
                    k: {f: v for f, v in value.items() if f != "changed_ids"}
                    for k, value in result["original_records"].items()
                },
                "alert_observation_text": [
                    {
                        "asset_id": r["asset_id"],
                        "caption": r["caption"],
                        "ocr_text": r["ocr_text"],
                        "detail_json": r["detail_json"],
                    }
                    for r in frame_rows
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
