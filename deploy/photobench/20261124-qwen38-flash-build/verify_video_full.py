"""Read-only verification of the frozen recovery set and content gaps."""

import json
import time
from collections import Counter
from urllib.parse import urlencode

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
    deployment = json.loads((ROOT / "deployment.json").read_text())
    result = {"observed_at": time.time(), "scope_id": SCOPE, "read_only": True}
    result["protected_pids"] = processes()
    assert result["protected_pids"] == deployment["protected_pids"]
    result["runtime_pids"] = {
        unit: systemctl("show", unit, "-p", "MainPID", "--value")
        for unit in (PRODUCT_UNIT, EVAL_UNIT, GATEWAY_UNIT)
    }
    result["gateway"] = call(18138, "/health")
    result["evaluation_work"] = call(8874, "/api/v1/active-work")
    assert not result["evaluation_work"]["work"]
    result["videos"] = []
    result["new_scene_events"] = []
    with connect() as connection:
        for row in rows:
            validate_preserved(connection, row)
            current = coverage(connection, row["asset_id"])
            assert_complete(current, row)
            job_id = "vjob_repair38_" + row["asset_id"].removeprefix("asset_")
            job = dict(connection.execute("SELECT * FROM video_jobs WHERE job_id=?", (job_id,)).fetchone())
            stages = [
                dict(r)
                for r in connection.execute("SELECT * FROM video_job_stages WHERE job_id=?", (job_id,))
            ]
            assert job["status"] == "COMPLETED" and stages
            assert all(stage["status"] == "SUCCEEDED" for stage in stages)
            for frame, scene in zip(current["frames"], current["scenes"], strict=True):
                frame["lexical_rows"] = connection.execute(
                    "SELECT count(*) FROM observation_search_fts WHERE asset_id=?", (frame["id"],)
                ).fetchone()[0]
                assert frame["lexical_rows"] > 0
                linked = connection.execute(
                    "SELECT count(*) FROM event_observations eo JOIN observations o "
                    "ON o.id=eo.observation_id WHERE eo.event_id=? AND o.asset_id=?",
                    (scene["id"], frame["id"]),
                ).fetchone()[0]
                assert linked > 0
                result["new_scene_events"].append(
                    dict(connection.execute("SELECT * FROM events WHERE id=?", (scene["id"],)).fetchone())
                )
            result["videos"].append({"coverage": current, "job": job, "stages": stages})
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
        for value in result["original_records"].values():
            assert value["missing"] == value["changed_count"] == 0
        result["image_content_gaps"] = [
            dict(r)
            for r in connection.execute(
                "SELECT a.id,a.path,a.parent_asset_id,o.id AS observation_id,o.caption,o.activity,"
                "o.ocr_text,o.detail_json FROM assets a JOIN observations o ON o.asset_id=a.id "
                "WHERE a.scope_id=? AND a.media_type='image' AND trim(coalesce(o.caption,''))=''",
                (SCOPE,),
            )
        ]
        result["review_frames"] = [
            dict(r)
            for r in connection.execute(
                "SELECT a.id,a.path,a.content_sha256,a.parent_asset_id,o.* FROM assets a "
                "JOIN observations o ON o.asset_id=a.id WHERE a.id IN (?,?)",
                ("asset_ad16dcecda08", "asset_042fdf27e328"),
            )
        ]
        result["graph_tables"] = {
            r[0]: connection.execute(f'SELECT count(*) FROM "{r[0]}"').fetchone()[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "(name LIKE '%graph%' OR name LIKE '%causal%')"
            )
        }
    result["searches"] = []
    for index in (0, len(rows) // 2, len(rows) - 1):
        video = result["videos"][index]["coverage"]
        with connect() as connection:
            caption = connection.execute(
                "SELECT caption FROM observations WHERE asset_id=?", (video["frames"][0]["id"],)
            ).fetchone()[0]
        search = call(
            8294,
            "/api/v1/search?" + urlencode({"q": caption[:80], "scope_id": SCOPE, "limit": 20}),
            timeout=120,
        )
        hit = any(item.get("asset", {}).get("id") == video["asset_id"] for item in search.get("items", []))
        result["searches"].append(
            {"asset_id": video["asset_id"], "query": caption[:80], "hit": hit, "response": search}
        )
    intent = json.loads((ROOT / "deployment-intent.json").read_text())
    requests = [json.loads(line) for line in (BASE / "model-requests.jsonl").read_text().splitlines()]
    requests = [r for r in requests if r["started_at"] >= intent["at"]]
    result["requests"] = {
        "total": len(requests),
        "statuses": dict(Counter(str(r.get("http_status")) for r in requests)),
        "length_by_budget_and_modality": dict(
            Counter(
                str((r.get("max_tokens"), bool(r.get("has_image"))))
                for r in requests
                if "length" in r.get("finish_reasons", [])
            )
        ),
        "failed": [r for r in requests if r.get("http_status") != 200],
        "configuration_mismatches": [
            r
            for r in requests
            if r.get("enable_thinking") is not False
            or (r.get("http_status") == 200 and r.get("actual_model") != "qwen3.8-flash")
        ],
    }
    assert not result["requests"]["configuration_mismatches"]
    result["structural_status"] = "accepted"
    result["content_status"] = "pending_visual_and_scene_summary_repair"
    save(ROOT / "full-recovery-acceptance.json", result)
    print(
        json.dumps(
            {
                "structural_status": result["structural_status"],
                "videos": len(rows),
                "frames": sum(len(v["coverage"]["frames"]) for v in result["videos"]),
                "scene_events": len(result["new_scene_events"]),
                "empty_captions": len(result["image_content_gaps"]),
                "requests": {
                    k: v
                    for k, v in result["requests"].items()
                    if k not in {"failed", "configuration_mismatches"}
                },
                "searches": [{"asset_id": r["asset_id"], "hit": r["hit"]} for r in result["searches"]],
            }
        )
    )


if __name__ == "__main__":
    main()
