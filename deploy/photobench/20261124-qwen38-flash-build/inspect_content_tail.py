"""Read residual content and derived records before any local correction."""

import argparse
import json
from pathlib import Path

from operate import call
from recover_videos import SCOPE, connect, save
from repair_content import CONTENT

REVIEW_IDS = (
    "asset_78a95b5d74d8",
    "asset_816c96df9415",
    "asset_abd37b0e8d4c",
    "asset_75deb3249002",
)


def inspect(output):
    from sentrix.application.memories.summarize_event import PLACEHOLDER_TITLES

    plan = json.loads((CONTENT / "plan.json").read_text())
    result = {"read_only": True, "review": {}, "partial": {}, "linked_events": []}
    with connect() as connection:
        schemas = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
        result["schemas"] = {r["name"]: r["sql"] for r in schemas}
        for identifier in (*REVIEW_IDS, *plan["partial_caption_gaps_deferred"]):
            asset = dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
            observations = [
                dict(r)
                for r in connection.execute("SELECT * FROM observations WHERE asset_id=?", (identifier,))
            ]
            row = {"asset": asset, "observations": observations, "derived": {}}
            for observation in observations:
                oid = observation["id"]
                assert observation["scope_id"] == SCOPE
                mentions = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT em.*,e.canonical_name,e.entity_type,e.status FROM entity_mentions em "
                        "JOIN entities e ON e.id=em.entity_id WHERE observation_id=?",
                        (oid,),
                    )
                ]
                row["derived"][oid] = {"entity_mentions": mentions}
                row["derived"][oid]["entity_observations"] = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT eo.*,e.canonical_name,e.status FROM entity_observations eo "
                        "JOIN entities e ON e.id=eo.entity_id WHERE observation_id=?",
                        (oid,),
                    )
                ]
                for table in ("facts", "entity_properties", "relationships", "event_entities"):
                    row["derived"][oid][table] = [
                        dict(r)
                        for r in connection.execute(
                            f"SELECT * FROM {table} WHERE evidence_ids_json LIKE ?", (f'%"{oid}"%',)
                        )
                    ]
            result["review" if identifier in REVIEW_IDS else "partial"][identifier] = row
        event_ids = sorted({e for t in plan["visual_targets"] for e in t["event_ids"]})
        for identifier in event_ids:
            event = dict(connection.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone())
            observations = [
                dict(r)
                for r in connection.execute(
                    "SELECT o.* FROM observations o JOIN event_observations eo ON eo.observation_id=o.id "
                    "WHERE eo.event_id=?",
                    (identifier,),
                )
            ]
            result["linked_events"].append({"event": event, "observations": observations})
        result["summary_gaps"] = [
            dict(r)
            for r in connection.execute(
                "SELECT id,title,summary,source_type,source_asset_id FROM events WHERE scope_id=? "
                "AND (trim(summary)='' OR title IN (?,?,?) OR "
                "(source_type='video_scene' AND summary LIKE '视频场景 · %'))",
                (SCOPE, *sorted(PLACEHOLDER_TITLES)),
            )
        ]
        result["scope_counts"] = {
            table: connection.execute(f"SELECT count(*) FROM {table} WHERE scope_id=?", (SCOPE,)).fetchone()[
                0
            ]
            for table in ("assets", "observations", "events", "facts", "entities")
        }
        result["graph_build"] = [
            dict(r)
            for r in connection.execute("SELECT * FROM memory_graph_builds WHERE scope_id=?", (SCOPE,))
        ]
        result["event_graph_mismatches"] = []
        for node in connection.execute(
            "SELECT id,label,attributes_json FROM memory_graph_nodes WHERE scope_id=? AND node_type='event'",
            (SCOPE,),
        ):
            attributes = json.loads(node["attributes_json"])
            identifier = attributes.get("event_id", node["id"].removeprefix("event:"))
            event = connection.execute(
                "SELECT title,summary FROM events WHERE id=?", (identifier,)
            ).fetchone()
            if event is not None and (
                node["label"] != event["title"] or attributes.get("summary") != event["summary"]
            ):
                result["event_graph_mismatches"].append(identifier)
    result["gateway"] = call(18138, "/health")
    result["active_work"] = call(8874, "/api/v1/active-work")
    save(output, result)
    print(
        json.dumps(
            {
                "partial": len(result["partial"]),
                "linked_events": len(event_ids),
                "summary_gaps": len(result["summary_gaps"]),
                "output": str(output),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    inspect(parser.parse_args().output)
