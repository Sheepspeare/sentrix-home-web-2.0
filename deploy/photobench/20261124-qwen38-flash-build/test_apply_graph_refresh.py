"""Publishing graph repairs preserves raw causal evidence and rolls back as a unit."""

import json
import sqlite3
from pathlib import Path

import apply_graph_refresh as publish
import pytest
import stage_graph_refresh as stage


def fixture():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    migration = (
        Path(__file__).parents[3]
        / "apps/api/src/sentrix/infrastructure/persistence/migrations/0014_memory_graph.sql"
    )
    connection.executescript(migration.read_text())
    for identifier in ("a", "b", "unchanged"):
        connection.execute(
            "INSERT INTO memory_graph_nodes VALUES (?,?,?,?,?,?,?,?)",
            (identifier, "scope", "event", identifier, '{"summary":"before"}', "old", "created", "updated"),
        )
    connection.execute(
        "INSERT INTO memory_graph_edges VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "causal",
            "scope",
            "a",
            "b",
            "asset_causes_asset:LEADS_TO",
            0.8,
            '{"raw_reason":"keep byte order"}',
            "old",
            "created",
            "updated",
        ),
    )
    connection.execute(
        "INSERT INTO memory_graph_edges VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "obsolete",
            "scope",
            "a",
            "b",
            "image_shares_object",
            0.9,
            '{"label":"old"}',
            "old",
            "created",
            "updated",
        ),
    )
    connection.execute("INSERT INTO memory_graph_builds VALUES (?,?,?,?,?)", ("scope", "old", 3, 2, "built"))
    connection.commit()
    current = publish.graph_rows(connection, "memory_graph_nodes", "scope")
    added = {"id": "new", "scope_id": "scope", "node_type": "fact", "label": "new fact", "attributes": {}}
    updated = stage.canonical(current["a"], "node")
    updated["attributes"] = {"summary": "repaired"}
    nodes = stage.graph_delta(
        current,
        [stage.canonical(r, "node") for k, r in current.items() if k != "a"] + [updated, added],
        "node",
    )
    edges = stage.graph_delta(publish.graph_rows(connection, "memory_graph_edges", "scope"), [], "edge")
    build = [dict(r) for r in connection.execute("SELECT * FROM memory_graph_builds")]
    return connection, {"nodes": nodes, "edges": edges}, build


def snapshot(connection):
    return [
        tuple(r)
        for table in ("memory_graph_nodes", "memory_graph_edges", "memory_graph_builds")
        for r in connection.execute(f"SELECT * FROM {table}")
    ]


def test_partial_publish_preserves_causal_and_unaffected_rows_and_updates_counts():
    connection, proposal, build = fixture()
    before = publish.graph_rows(connection, "memory_graph_nodes", "scope")
    causal = publish.graph_rows(connection, "memory_graph_edges", "scope")["causal"]
    with connection:
        result = publish.apply_delta(connection, proposal, build, "patch", "now", scope="scope")
    after = publish.graph_rows(connection, "memory_graph_nodes", "scope")
    assert result == {"nodes": 4, "edges": 1, "causal_rows_preserved": 1, "unaffected_rows_preserved": True}
    assert after["unchanged"] == before["unchanged"]
    assert after["a"]["created_at"] == "created" and after["a"]["build_revision"] == "patch"
    assert json.loads(after["a"]["attributes_json"])["summary"] == "repaired"
    assert publish.graph_rows(connection, "memory_graph_edges", "scope") == {"causal": causal}
    assert tuple(connection.execute("SELECT * FROM memory_graph_builds").fetchone()) == (
        "scope",
        "patch",
        4,
        1,
        "now",
    )


def test_write_failure_rolls_back_deletions_inserts_and_updates():
    connection, proposal, build = fixture()
    before = snapshot(connection)
    connection.executescript(
        "CREATE TRIGGER reject_update BEFORE UPDATE ON memory_graph_nodes "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"), connection:
        publish.apply_delta(connection, proposal, build, "patch", "now", scope="scope")
    assert snapshot(connection) == before


def test_stale_target_refuses_publish():
    connection, proposal, build = fixture()
    connection.execute("UPDATE memory_graph_nodes SET label='external' WHERE id='a'")
    connection.commit()
    before = snapshot(connection)
    with pytest.raises(AssertionError, match="Staged update changed"), connection:
        publish.apply_delta(connection, proposal, build, "patch", "now", scope="scope")
    assert snapshot(connection) == before


def test_missing_endpoint_refuses_publish():
    connection, proposal, build = fixture()
    proposal["edges"]["add"].append(
        {
            "id": "dangling",
            "scope_id": "scope",
            "source_id": "a",
            "target_id": "missing",
            "relation": "image_shares_object",
            "weight": 0.9,
            "attributes": {},
        }
    )
    proposal["edges"]["desired_count"] += 1
    before = snapshot(connection)
    with pytest.raises(AssertionError, match="endpoint missing"), connection:
        publish.apply_delta(connection, proposal, build, "patch", "now", scope="scope")
    assert snapshot(connection) == before


def test_existing_causal_edge_cannot_be_removed():
    connection, proposal, build = fixture()
    causal = publish.graph_rows(connection, "memory_graph_edges", "scope")["causal"]
    proposal["edges"]["remove"].append(causal)
    proposal["edges"]["desired_count"] = 0
    before = snapshot(connection)
    with pytest.raises(AssertionError), connection:
        publish.apply_delta(connection, proposal, build, "patch", "now", scope="scope")
    assert snapshot(connection) == before
