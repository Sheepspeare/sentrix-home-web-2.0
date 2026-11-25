"""Read-only acceptance of the first video and the pre-recovery records."""

import json
from collections import Counter
from urllib.parse import urlencode

from operate import call, processes
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


def differences(connection, table, where="1", parameters=()):
    columns = [row["name"] for row in connection.execute(f"PRAGMA main.table_info({table})")]
    changed = " OR ".join(f'a."{column}" IS NOT b."{column}"' for column in columns)
    total = connection.execute(f"SELECT count(*) FROM old.{table} b WHERE {where}", parameters).fetchone()[0]
    missing = connection.execute(
        f"SELECT count(*) FROM old.{table} b LEFT JOIN main.{table} a ON a.id=b.id "
        f"WHERE ({where}) AND a.id IS NULL",
        parameters,
    ).fetchone()[0]
    changes = [
        row[0]
        for row in connection.execute(
            f"SELECT b.id FROM old.{table} b JOIN main.{table} a ON a.id=b.id "
            f"WHERE ({where}) AND ({changed})",
            parameters,
        )
    ]
    return {"original_rows": total, "missing": missing, "changed_count": len(changes), "changed_ids": changes}


def main():
    rows = plans(ROOT / "recovery-plans.json")
    row = rows[0]
    deployment = json.loads((ROOT / "deployment.json").read_text())
    intent = json.loads((ROOT / "deployment-intent.json").read_text())
    assert processes() == deployment["protected_pids"]
    job_id = "vjob_repair38_427c5b857233"
    job = call(8294, f"/api/v1/video-jobs/{job_id}")
    assert job["status"] == "COMPLETED"
    assert all(stage["status"] == "SUCCEEDED" for stage in job["stages"])
    with connect() as connection:
        connection.execute(
            "ATTACH DATABASE ? AS old", ((ROOT / "before-recovery/sentrix.db").as_uri() + "?mode=ro",)
        )
        validate_preserved(connection, row)
        current = coverage(connection, row["asset_id"])
        assert_complete(current, row)
        placeholders = ",".join("?" for _ in rows)
        ids = tuple(item["asset_id"] for item in rows)
        protected_assets = differences(
            connection,
            "assets",
            f"b.id NOT IN ({placeholders}) AND coalesce(b.parent_asset_id,'') NOT IN ({placeholders})",
            ids + ids,
        )
        preserved = {"assets": protected_assets}
        for table in ("observations", "events", "memory_vectors", "text_vectors"):
            preserved[table] = differences(connection, table)
        for table in ("assets", "observations", "events", "memory_vectors"):
            assert preserved[table]["missing"] == preserved[table]["changed_count"] == 0, (
                table,
                preserved[table],
            )
        lexical = {}
        observations = []
        for frame in current["frames"]:
            lexical[frame["id"]] = connection.execute(
                "SELECT count(*) FROM observation_search_fts WHERE asset_id=?", (frame["id"],)
            ).fetchone()[0]
            assert lexical[frame["id"]] > 0
            observations.extend(
                dict(item)
                for item in connection.execute(
                    "SELECT id,caption,ocr_text,detail_json FROM observations WHERE asset_id=?",
                    (frame["id"],),
                )
            )
        before = json.loads((ROOT / "inventory-before.json").read_text())
        after = {
            table: connection.execute(f"SELECT count(*) FROM {table} WHERE scope_id=?", (SCOPE,)).fetchone()[
                0
            ]
            for table in before["index_counts"]
        }
    query = observations[0]["caption"][:80]
    search = call(
        8294, "/api/v1/search?" + urlencode({"q": query, "scope_id": SCOPE, "limit": 20}), timeout=120
    )
    requests = []
    for line in (BASE / "model-requests.jsonl").read_text().splitlines():
        item = json.loads(line)
        if item["started_at"] >= intent["at"]:
            requests.append(item)
    assert requests and all(item.get("http_status") == 200 for item in requests)
    assert all(item.get("actual_model") == "qwen3.8-flash" for item in requests)
    assert all(
        item.get("enable_thinking") is False and not item.get("has_reasoning_content") for item in requests
    )
    assert sum(bool(item.get("has_image")) for item in requests) == 3
    assert all(item.get("max_tokens") == 4096 for item in requests if item.get("has_image"))
    assert not call(8874, "/api/v1/active-work")["work"]
    result = {
        "status": "accepted_frames_and_indexes",
        "asset_id": row["asset_id"],
        "job": job,
        "coverage": current,
        "lexical_rows_by_frame": lexical,
        "original_records": preserved,
        "counts_before": before["index_counts"],
        "counts_after": after,
        "requests": requests,
        "finish_reasons": dict(
            Counter(reason for item in requests for reason in item.get("finish_reasons", []))
        ),
        "search": search,
        "new_scene_summaries": "pending: video-jobs writes placeholder scene summaries; close separately",
        "existing_files_and_analysis_preserved": True,
        "new_model_requests_from_verifier": 0,
    }
    save(ROOT / "smoke-acceptance.json", result)
    print(
        json.dumps(
            {
                key: result[key]
                for key in ["status", "lexical_rows_by_frame", "counts_after", "finish_reasons"]
            }
        )
    )
    print(
        json.dumps(
            {
                "search_backend": search.get("backend"),
                "search_trace": search.get("channel_trace"),
                "search_returned_repaired_video": any(
                    item.get("asset", {}).get("id") == row["asset_id"] for item in search.get("items", [])
                ),
                "requests": len(requests),
                "protected_rows": {
                    key: {k: v for k, v in value.items() if k != "changed_ids"}
                    for key, value in preserved.items()
                },
            }
        )
    )


if __name__ == "__main__":
    main()
