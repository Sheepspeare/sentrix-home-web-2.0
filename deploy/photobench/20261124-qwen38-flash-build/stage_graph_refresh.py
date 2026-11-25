"""Stage a read-only graph delta from current production projections and saved causal edges."""

import json
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime

import finish_photo_content as photos
import repair_content as repair
from recover_videos import BASE, DATA, ROOT, SCOPE, connect, save, sha

OUTPUT = repair.CONTENT / "graph-refresh"


def canonical(row, kind):
    keys = (
        ("id", "scope_id", "node_type", "label")
        if kind == "node"
        else (
            "id",
            "scope_id",
            "source_id",
            "target_id",
            "relation",
            "weight",
        )
    )
    return {**{key: row[key] for key in keys}, "attributes": json.loads(row["attributes_json"])}


def projected(value, kind):
    row = asdict(value)
    if kind == "node":
        row["node_type"] = row.pop("kind")
    return row


def graph_delta(current, desired, kind):
    """Archive only changed rows; keep successful causal edges byte-for-byte."""
    proposed = {row["id"]: row for row in desired}
    if kind == "edge":
        for identifier, row in current.items():
            if row["relation"].startswith("asset_causes_asset:"):
                assert identifier not in proposed
                proposed[identifier] = canonical(row, kind)
    return {
        "add": [row for identifier, row in proposed.items() if identifier not in current],
        "remove": [row for identifier, row in current.items() if identifier not in proposed],
        "update": [
            {"before": current[identifier], "after": row}
            for identifier, row in proposed.items()
            if identifier in current and canonical(current[identifier], kind) != row
        ],
        "desired_count": len(proposed),
    }


class CaptureGraph:
    """Receive a production projection without publishing it."""

    def replace_scope(self, scope_id, *, nodes, edges, build_revision):
        from sentrix.domain.memories.graph import GraphBuildStats

        assert scope_id == SCOPE
        self.nodes, self.edges = nodes, edges
        self.revision = build_revision
        return GraphBuildStats(scope_id=scope_id, node_count=len(nodes), edge_count=len(edges))


def request_audit():
    job = json.loads((ROOT / "full-resume-2/vjob_repair38_4e13da42fd53-terminal.json").read_text())
    stage = next(row for row in job["stages"] if row["stage"] == "MEMORY_GRAPH")
    start, end = (datetime.fromisoformat(stage[key]).timestamp() for key in ("started_at", "finished_at"))
    all_counts, errors, final = Counter(), [], []
    for line in (BASE / "model-requests.jsonl").open():
        row = json.loads(line)
        status = row.get("http_status")
        finishes = ",".join(row.get("finish_reasons") or [])
        category = "health" if row.get("max_tokens") == 8 else "image" if row.get("has_image") else "text"
        all_counts[f"{category}/{status}/{finishes}"] += 1
        if status != 200:
            errors.append(row)
        if start <= row["started_at"] <= end:
            final.append(row)
    return {
        "final_graph_job": job["job_id"],
        "stage": stage,
        "stage_requests": final,
        "stage_errors": [r for r in final if r.get("http_status") != 200],
        "stage_non_health_length": [
            r for r in final if r.get("max_tokens") != 8 and "length" in r.get("finish_reasons", [])
        ],
        "all_request_counts_by_purpose": dict(all_counts),
        "all_http_failures": errors,
        "purpose_limit": (
            "Final stage requests selected by exact saved stage timestamps; "
            "successful causal verdicts are preserved, not inferred from counts."
        ),
    }


def main():
    from sentrix.application.memories.build_graph import BuildMemoryGraph
    from sentrix.infrastructure.persistence.database import Database
    from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository

    repair.assert_idle()
    assert not OUTPUT.exists(), "Inspect staged delta before starting another projection"
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    OUTPUT.mkdir()
    started = time.time()
    empty_plan = {"caption_before": {}, "stale_events": [], "caption_event_ids": {}}
    try:
        with connect() as connection:
            before = photos.protected_fingerprints(connection, empty_plan)
            nodes = {
                r["id"]: dict(r)
                for r in connection.execute("SELECT * FROM memory_graph_nodes WHERE scope_id=?", (SCOPE,))
            }
            edges = {
                r["id"]: dict(r)
                for r in connection.execute("SELECT * FROM memory_graph_edges WHERE scope_id=?", (SCOPE,))
            }
            builds = [
                dict(r)
                for r in connection.execute("SELECT * FROM memory_graph_builds WHERE scope_id=?", (SCOPE,))
            ]
        save(OUTPUT / "before.json", {"table_fingerprints": before, "graph_build": builds})
        repos = repair.repositories()
        capture = CaptureGraph()
        database = Database(DATA / "sentrix.db", DATA / "unused-migrations")
        stats = BuildMemoryGraph(
            assets=repos.assets,
            observations=repos.observations,
            events=repos.events,
            participants=repos.participants,
            entities=repos.entities,
            facts=repos.facts,
            relationships=repos.relationships,
            graph=capture,
            causal_judge=None,
            vectors=SqliteVectorRepository(database),
        ).execute(SCOPE)
        delta = {
            "scope_id": SCOPE,
            "read_only": True,
            "new_model_requests": 0,
            "nodes": graph_delta(nodes, [projected(n, "node") for n in capture.nodes], "node"),
            "edges": graph_delta(edges, [projected(e, "edge") for e in capture.edges], "edge"),
            "production_stats_without_causal_rerun": asdict(stats),
            "original_causal_count": sum(
                r["relation"].startswith("asset_causes_asset:") for r in edges.values()
            ),
            "source_script_sha256": sha(__file__),
        }
        save(OUTPUT / "proposal.json", delta)
        save(OUTPUT / "request-audit.json", request_audit())
        with connect() as connection:
            assert photos.protected_fingerprints(connection, empty_plan) == before
        summary = {
            "status": "staged_read_only",
            "new_model_requests": 0,
            "original_causal_count_preserved": delta["original_causal_count"],
            "changes": {
                kind: {action: len(delta[kind][action]) for action in ("add", "remove", "update")}
                for kind in ("nodes", "edges")
            },
            "started_at": started,
            "finished_at": time.time(),
            "proposal_sha256": sha(OUTPUT / "proposal.json"),
        }
    except Exception as error:
        summary = {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "started_at": started,
            "finished_at": time.time(),
        }
        raise
    finally:
        save(OUTPUT / "terminal.json", summary)
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
