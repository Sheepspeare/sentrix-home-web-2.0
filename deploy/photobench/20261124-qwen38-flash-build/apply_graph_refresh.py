"""Review and atomically publish the saved production graph delta only."""

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime

import finish_photo_content as photos
import repair_content as repair
import stage_graph_refresh as stage
from recover_videos import DATA, SCOPE, connect, save, sha

DIRECTORY = stage.OUTPUT
PROPOSAL_SHA = "7fbffd1da17167d20f21ff841e4de47aca89f2200b5830ad71dfb174b3f92711"
EMPTY_PLAN = {"caption_before": {}, "stale_events": [], "caption_event_ids": {}}


def graph_rows(connection, table, scope=SCOPE):
    return {r["id"]: dict(r) for r in connection.execute(f"SELECT * FROM {table} WHERE scope_id=?", (scope,))}


def unchanged_rows(rows, changes):
    targets = {r["id"] for r in changes["add"] + changes["remove"]}
    targets.update(r["after"]["id"] for r in changes["update"])
    digest = hashlib.sha256()
    for identifier in sorted(rows.keys() - targets):
        digest.update(json.dumps(rows[identifier], sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()


def apply_delta(connection, proposal, original_build, revision, timestamp, *, scope=SCOPE):
    """Use the caller's transaction; never rewrite unaffected or causal records."""
    current = {
        kind: graph_rows(connection, table, scope)
        for kind, table in (("nodes", "memory_graph_nodes"), ("edges", "memory_graph_edges"))
    }
    builds = [
        dict(r) for r in connection.execute("SELECT * FROM memory_graph_builds WHERE scope_id=?", (scope,))
    ]
    assert builds == original_build, "Graph build changed after staging"
    causal = {k: r for k, r in current["edges"].items() if r["relation"].startswith("asset_causes_asset:")}
    desired = {}
    for kind, singular in (("nodes", "node"), ("edges", "edge")):
        rows, changes = current[kind], proposal[kind]
        desired[kind] = {k: stage.canonical(r, singular) for k, r in rows.items()}
        for row in changes["add"]:
            assert row["scope_id"] == scope and row["id"] not in rows
            desired[kind][row["id"]] = row
        for row in changes["remove"]:
            assert rows.get(row["id"]) == row, "Staged removal changed"
            desired[kind].pop(row["id"])
        for item in changes["update"]:
            row, after = item["before"], item["after"]
            assert rows.get(row["id"]) == row, "Staged update changed"
            assert after["id"] == row["id"] and after["scope_id"] == scope
            desired[kind][row["id"]] = after
        assert len(desired[kind]) == changes["desired_count"]
    assert all(
        e["source_id"] in desired["nodes"] and e["target_id"] in desired["nodes"]
        for e in desired["edges"].values()
    ), "Proposed edge endpoint missing"
    for identifier, row in causal.items():
        assert desired["edges"].get(identifier) == stage.canonical(row, "edge")
        assert identifier not in {r["id"] for r in proposal["edges"]["remove"]}
        assert identifier not in {r["after"]["id"] for r in proposal["edges"]["update"]}

    for kind, table in (("edges", "memory_graph_edges"), ("nodes", "memory_graph_nodes")):
        for row in proposal[kind]["remove"]:
            connection.execute(f"DELETE FROM {table} WHERE id=? AND scope_id=?", (row["id"], scope))
    for kind, table in (("nodes", "memory_graph_nodes"), ("edges", "memory_graph_edges")):
        keys = ("node_type", "label") if kind == "nodes" else ("source_id", "target_id", "relation", "weight")
        for row in proposal[kind]["add"]:
            columns = (
                "id",
                "scope_id",
                *keys,
                "attributes_json",
                "build_revision",
                "created_at",
                "updated_at",
            )
            values = [
                row["id"],
                scope,
                *(row[k] for k in keys),
                json.dumps(row["attributes"], ensure_ascii=False, sort_keys=True),
                revision,
                timestamp,
                timestamp,
            ]
            connection.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values
            )
        for item in proposal[kind]["update"]:
            row = item["after"]
            columns = (*keys, "attributes_json", "build_revision", "updated_at")
            values = [
                *(row[k] for k in keys),
                json.dumps(row["attributes"], ensure_ascii=False, sort_keys=True),
                revision,
                timestamp,
                row["id"],
                scope,
            ]
            connection.execute(
                f"UPDATE {table} SET {','.join(k + '=?' for k in columns)} WHERE id=? AND scope_id=?", values
            )
    connection.execute(
        "UPDATE memory_graph_builds SET build_revision=?,node_count=?,edge_count=?,built_at=? "
        "WHERE scope_id=?",
        (revision, len(desired["nodes"]), len(desired["edges"]), timestamp, scope),
    )
    verification = {}
    for kind, table in (("nodes", "memory_graph_nodes"), ("edges", "memory_graph_edges")):
        after = graph_rows(connection, table, scope)
        singular = "node" if kind == "nodes" else "edge"
        assert {k: stage.canonical(r, singular) for k, r in after.items()} == desired[kind]
        assert unchanged_rows(current[kind], proposal[kind]) == unchanged_rows(after, proposal[kind])
        verification[kind] = len(after)
    assert {k: after[k] for k in causal} == causal
    return {**verification, "causal_rows_preserved": len(causal), "unaffected_rows_preserved": True}


def review_provenance(connection, proposal):
    """Attribute changes to frozen repairs, including displaced neighborhood edges."""
    from sentrix.application.memories.image_graph import project_image_graph
    from sentrix.application.memories.keyframe_graph import project_keyframe_graph
    from sentrix.infrastructure.persistence.database import Database
    from sentrix.infrastructure.persistence.repositories.assets import SqliteObservationRepository

    content = json.loads((repair.CONTENT / "plan.json").read_text())
    legacy = json.loads((repair.CONTENT / "legacy-scene-summaries/plan.json").read_text())
    photo = json.loads((photos.OUTPUT / "plan.json").read_text())
    summary_ids = {"event:" + r["id"] for r in content["scene_events"] + legacy["scene_events"]}
    summary_ids.update("event:" + r["event"]["id"] for r in photo["stale_events"])
    visual_ids = {r["before"]["id"] for r in content["visual_targets"]}
    observation_ids = visual_ids | {r["id"] for r in photo["caption_before"].values()}
    updates = {r["after"]["id"]: r for r in proposal["nodes"]["update"]}
    observation_updates = {k: r for k, r in updates.items() if r["after"]["node_type"] == "observation"}
    assert set(observation_updates) == {"observation:" + k for k in observation_ids}
    changed_summaries, bindings = set(), []
    for identifier, item in updates.items():
        after = item["after"]
        if after["node_type"] != "event":
            continue
        before = stage.canonical(item["before"], "node")
        if (
            any(
                before["attributes"][k] != after["attributes"][k]
                for k in ("summary", "activity", "event_type")
            )
            or before["label"] != after["label"]
        ):
            changed_summaries.add(identifier)
        if before["attributes"]["event_entity_ids"] != after["attributes"]["event_entity_ids"]:
            entity_rows = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM event_entities WHERE event_id=? ORDER BY relation,entity_id",
                    (after["attributes"]["event_id"],),
                )
            ]
            assert after["attributes"]["event_entity_ids"] == [r["entity_id"] for r in entity_rows]
            evidence_observations = set(after["attributes"]["observation_ids"]) & visual_ids
            assert evidence_observations, "Changed entity binding unrelated to repaired observations"
            bindings.append(
                {
                    "event_id": identifier,
                    "repaired_observations": sorted(evidence_observations),
                    "current_bindings": entity_rows,
                }
            )
    assert changed_summaries == summary_ids and len(summary_ids) == 2291
    assert not proposal["nodes"]["remove"]
    assert all(r["node_type"] in ("fact", "concept") for r in proposal["nodes"]["add"])
    facts = [r for r in proposal["nodes"]["add"] if r["node_type"] == "fact"]
    assert all(r["attributes"]["observation_id"] in visual_ids for r in facts)
    concept_ids = {r["id"] for r in proposal["nodes"]["add"] if r["node_type"] == "concept"}
    concept_edges = [r for r in proposal["edges"]["add"] if r["relation"] == "observation_mentions_concept"]
    assert concept_ids <= {r["target_id"] for r in concept_edges}
    assert all(r["source_id"] in observation_updates for r in concept_edges)

    repos = repair.repositories()
    assets = repos.assets.list_by_scope(SCOPE, limit=1000000)
    current_observations = repos.observations.list_scope(SCOPE, limit=1000000)
    old_db = Database(repair.CONTENT / "before-content.db", DATA / "unused-migrations")
    old_observations = SqliteObservationRepository(old_db).list_scope(SCOPE, limit=1000000)
    latest = {r.asset_id: r for r in sorted(current_observations, key=lambda o: (o.revision, o.updated_at))}
    original = {r.asset_id: r for r in sorted(old_observations, key=lambda o: (o.revision, o.updated_at))}
    for item in observation_updates.values():
        old = json.loads(item["before"]["attributes_json"])
        source = original[old["asset_id"]]
        assert list(source.objects) == old["objects"]
        assert [f"{f.subject} {f.predicate} {f.object}".strip() for f in source.facts] == old["fact_texts"]

    relations = {
        "image_shares_object",
        "frame_shares_object",
        "frame_shares_relation",
        "frame_mentions_object",
    }
    projections = []
    for observations in (original, latest):
        projected = set()

        def capture(source, target, relation, weight, attributes, discriminator, *, collected=projected):
            if relation in relations:
                collected.add((source, target, relation, attributes["label"]))

        project_image_graph(assets=assets, latest_observations=observations, vectors=None, add_edge=capture)
        project_keyframe_graph(
            assets=assets,
            latest_observations=observations,
            scene_by_asset={},
            vectors=None,
            add_node=lambda *args: None,
            add_edge=capture,
        )
        projections.append(projected)

    def edge_key(row):
        attrs = row.get("attributes")
        if attrs is None:
            attrs = json.loads(row["attributes_json"])
        return row["source_id"], row["target_id"], row["relation"], attrs["label"]

    expected_add, expected_remove = projections[1] - projections[0], projections[0] - projections[1]
    actual_add = {edge_key(r) for r in proposal["edges"]["add"] if r["relation"] in relations}
    actual_remove = {edge_key(r) for r in proposal["edges"]["remove"] if r["relation"] in relations}
    assert actual_add == expected_add and actual_remove == expected_remove
    assert (
        connection.execute("SELECT status FROM entities WHERE id='entity_3b03bd7b0e1e'").fetchone()[0]
        == "rejected"
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM memory_graph_nodes WHERE id='entity:entity_3b03bd7b0e1e'"
        ).fetchone()[0]
        == 0
    )
    request_audit = json.loads((DIRECTORY / "request-audit.json").read_text())
    assert not request_audit["stage_errors"] and not request_audit["stage_non_health_length"]
    counts = Counter(r["max_tokens"] for r in request_audit["stage_requests"])
    assert counts == {768: 40, 8: 4}
    return {
        "summary_updates": len(summary_ids),
        "binding_only_updates": sum(
            r["after"]["id"] not in summary_ids
            for r in updates.values()
            if r["after"]["node_type"] == "event"
        ),
        "binding_changes": bindings,
        "observation_updates": len(observation_updates),
        "new_fact_nodes": len(facts),
        "new_concept_nodes": len(concept_ids),
        "object_relation_changes_match_production_before_after": True,
        "neighborhood_additions": len(actual_add),
        "neighborhood_removals": len(actual_remove),
        "last_causal_batches_successful": 40,
        "original_causal_edges_preserved": proposal["original_causal_count"],
        "wrong_entity_never_published_in_original_graph": True,
        "new_model_requests": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("review", "apply"))
    args = parser.parse_args()
    repair.assert_idle()
    assert sha(DIRECTORY / "proposal.json") == PROPOSAL_SHA
    proposal = json.loads((DIRECTORY / "proposal.json").read_text())
    before = json.loads((DIRECTORY / "before.json").read_text())
    if args.mode == "review":
        with connect() as connection:
            assert photos.protected_fingerprints(connection, EMPTY_PLAN) == before["table_fingerprints"]
            result = review_provenance(connection, proposal)
            assert photos.protected_fingerprints(connection, EMPTY_PLAN) == before["table_fingerprints"]
        save(
            DIRECTORY / "review.json",
            {
                "status": "reviewed_read_only",
                "proposal_sha256": PROPOSAL_SHA,
                "script_sha256": sha(__file__),
                **result,
            },
        )
        print(json.dumps({k: v for k, v in result.items() if k != "binding_changes"}))
        return

    review = json.loads((DIRECTORY / "review.json").read_text())
    assert review["status"] == "reviewed_read_only" and review["proposal_sha256"] == PROPOSAL_SHA
    output = DIRECTORY / "apply"
    assert not output.exists(), "Inspect existing apply result before taking further action"
    output.mkdir()
    started = time.time()
    try:
        from sentrix.infrastructure.persistence.database import Database

        database = Database(DATA / "sentrix.db", DATA / "unused-migrations")
        with database.advisory_lock("graph-build"), database.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            assert photos.protected_fingerprints(connection, EMPTY_PLAN) == before["table_fingerprints"]
            revision = hashlib.sha256(
                (before["graph_build"][0]["build_revision"] + PROPOSAL_SHA).encode()
            ).hexdigest()
            save(
                output / "intent.json",
                {
                    "proposal_sha256": PROPOSAL_SHA,
                    "script_sha256": sha(__file__),
                    "revision": revision,
                    "before_build": before["graph_build"],
                    "review_sha256": sha(DIRECTORY / "review.json"),
                },
            )
            result = apply_delta(
                connection, proposal, before["graph_build"], revision, datetime.now(UTC).isoformat()
            )
            protected = photos.protected_fingerprints(connection, EMPTY_PLAN)
            assert {
                k: v for k, v in protected.items() if k not in ("memory_graph_nodes", "memory_graph_edges")
            } == {
                k: v
                for k, v in before["table_fingerprints"].items()
                if k not in ("memory_graph_nodes", "memory_graph_edges")
            }
            save(
                output / "verification.json",
                {
                    **result,
                    "protected_after": protected,
                    "non_graph_tables_preserved": True,
                    "row_revision_semantics": (
                        "Changed rows use patch revision; "
                        "unaffected rows retain original revision and timestamps"
                    ),
                },
            )
        with connect() as connection:
            assert photos.protected_fingerprints(connection, EMPTY_PLAN) == protected
        terminal = {
            "status": "completed",
            "started_at": started,
            "finished_at": time.time(),
            "new_model_requests": 0,
            "build_revision": revision,
            **result,
        }
    except Exception as error:
        terminal = {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "started_at": started,
            "finished_at": time.time(),
        }
        raise
    finally:
        save(output / "terminal.json", terminal)
        print(json.dumps(terminal))


if __name__ == "__main__":
    main()
