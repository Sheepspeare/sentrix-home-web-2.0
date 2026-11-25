"""Read-only acceptance of saved legacy summaries, raw calls and protected records."""

import argparse
import json
from pathlib import Path

import repair_content as repair
from operate import EVAL_UNIT, GATEWAY_UNIT, PRODUCT_UNIT, call, systemctl
from recover_videos import connect, save, sha
from repair_legacy_summaries import DIRECTORY
from verify_video_smoke import differences


def inspect(output):
    plan = json.loads((DIRECTORY / "plan.json").read_text())
    result = {
        "read_only": True,
        "plan_sha256": sha(DIRECTORY / "plan.json"),
        "saved": [],
        "unsaved_ids": [],
        "requests": [],
        "preserved": {},
    }
    with connect() as connection:
        connection.execute(
            "ATTACH DATABASE ? AS old", ((DIRECTORY / "before-legacy.db").as_uri() + "?mode=ro",)
        )
        for before in plan["scene_events"]:
            identifier = before["id"]
            current = dict(connection.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone())
            for observation in plan["observations"][identifier]:
                assert (
                    dict(
                        connection.execute(
                            "SELECT * FROM observations WHERE id=?", (observation["id"],)
                        ).fetchone()
                    )
                    == observation
                )
            directory = DIRECTORY / "summary" / identifier
            success_path = directory / "success.json"
            if not success_path.exists():
                result["unsaved_ids"].append(identifier)
                # Index failures can leave an applied summary with a reusable model response.
                if current != before:
                    result.setdefault("unsaved_modified", []).append(identifier)
                continue
            success = json.loads(success_path.read_text())
            assert current == success["after"] and current["revision"] == before["revision"] + 1
            changed = {key for key in current if current[key] != before[key]}
            assert changed <= {"title", "summary", "event_type", "activity", "revision", "updated_at"}
            assert current["summary"].strip() and not current["summary"].startswith("视频场景 · ")
            assert success["summary_model"] == repair.MODEL and success["text_dimension"] == 2048
            vectors = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM text_vectors WHERE source_type='event' "
                    "AND source_id=? AND space='episodic'",
                    (identifier,),
                )
            ]
            assert len(vectors) == 1 and vectors[0]["dimension"] == 2048
            assert len(json.loads(vectors[0]["vector_json"])) == 2048
            assert json.loads(vectors[0]["metadata_json"])["observation_id"] in {
                observation["id"] for observation in plan["observations"][identifier]
            }
            result["saved"].append(
                {
                    "id": identifier,
                    "title": current["title"],
                    "summary": current["summary"],
                    "changed_fields": sorted(changed),
                    "vector_id": vectors[0]["id"],
                    "success_sha256": sha(success_path),
                }
            )
        allowed = tuple(r["id"] for r in result["saved"]) + tuple(result.get("unsaved_modified", []))
        placeholders = ",".join("?" for _ in allowed)
        for table in (
            "assets",
            "observations",
            "memory_vectors",
            "events",
            "text_vectors",
            "facts",
            "entities",
            "entity_properties",
            "relationships",
            "memory_graph_nodes",
            "memory_graph_edges",
        ):
            where, parameters = "1", ()
            if table == "events" and allowed:
                where, parameters = f"b.id NOT IN ({placeholders})", allowed
            if table == "text_vectors" and allowed:
                where = (
                    f"NOT (b.source_type='event' AND b.source_id IN ({placeholders}) AND b.space='episodic')"
                )
                parameters = allowed
            delta = differences(connection, table, where, parameters)
            additions = [
                dict(r)
                for r in connection.execute(
                    f"SELECT a.* FROM main.{table} a LEFT JOIN old.{table} b ON a.id=b.id WHERE b.id IS NULL"
                )
            ]
            if additions:
                assert table == "text_vectors"
                assert all(
                    r["source_type"] == "event"
                    and r["source_id"] in allowed
                    and r["space"] == "episodic"
                    and r["dimension"] == 2048
                    for r in additions
                )
            delta["added_ids"] = [r["id"] for r in additions]
            result["preserved"][table] = delta
            assert delta["missing"] == delta["changed_count"] == 0, (table, delta)
        for table in (
            "entity_observations",
            "event_entities",
            "entity_mentions",
            "event_observations",
            "observation_search_fts",
        ):
            removed = connection.execute(
                f"SELECT count(*) FROM (SELECT * FROM old.{table} EXCEPT SELECT * FROM main.{table})"
            ).fetchone()[0]
            added = connection.execute(
                f"SELECT count(*) FROM (SELECT * FROM main.{table} EXCEPT SELECT * FROM old.{table})"
            ).fetchone()[0]
            result["preserved"][table] = {"removed": removed, "added": added}
            assert added == removed == 0
    for response_path in sorted((DIRECTORY / "summary").glob("*/model/attempt-*/response.json")):
        request = json.loads(response_path.with_name("request.json").read_text())
        raw = json.loads(response_path.read_text())
        assert request["model"] == repair.MODEL and request["enable_thinking"] is False
        assert request["max_tokens"] == 4096
        row = {
            "id": response_path.relative_to(DIRECTORY / "summary").parts[0],
            "path": str(response_path),
            "request_sha256": sha(response_path.with_name("request.json")),
            "response_sha256": sha(response_path),
            "status_code": raw["status_code"],
            "enable_thinking": False,
            "max_tokens": 4096,
        }
        if raw["status_code"] == 200:
            payload = json.loads(raw["body"])
            choice = payload["choices"][0]
            assert payload["model"] == repair.MODEL and not choice["message"].get("reasoning_content")
            row.update(
                model=payload["model"],
                finish_reason=choice["finish_reason"],
                usage=payload.get("usage"),
                content=choice["message"].get("content"),
            )
        else:
            row["error_body"] = raw["body"]
        result["requests"].append(row)
    result["runtime_pids"] = {
        unit: systemctl("show", unit, "-p", "MainPID", "--value")
        for unit in (PRODUCT_UNIT, EVAL_UNIT, GATEWAY_UNIT)
    }
    result["gateway"] = call(18138, "/health")
    result["active_work"] = call(8874, "/api/v1/active-work")
    result["counts"] = {
        "planned": len(plan["scene_events"]),
        "saved": len(result["saved"]),
        "unsaved": len(result["unsaved_ids"]),
        "responses": len(result["requests"]),
    }
    save(output, result)
    print(
        json.dumps(
            {
                "counts": result["counts"],
                "unsaved_modified": result.get("unsaved_modified", []),
                "runtime_pids": result["runtime_pids"],
                "gateway": result["gateway"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    inspect(parser.parse_args().output)
