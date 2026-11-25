"""Apply five reviewed captions and refresh eight frozen outdated photo summaries."""

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

import repair_content as repair
from recover_videos import SCOPE, connect, save, sha

CONTENT = repair.CONTENT
STAGED = CONTENT / "partial-captions"
OUTPUT = CONTENT / "photo-content-tail"
TABLES = (
    "assets",
    "observations",
    "events",
    "memory_vectors",
    "text_vectors",
    "facts",
    "entities",
    "entity_properties",
    "relationships",
    "memory_graph_nodes",
    "memory_graph_edges",
    "entity_observations",
    "event_entities",
    "entity_mentions",
    "event_observations",
    "observation_search_terms",
    "observation_search_fts",
)


def caption_semantics(original, caption):
    from sentrix.domain.media.ports import ObservationSemantics

    return ObservationSemantics(
        caption=caption,
        **{
            field: getattr(original, field)
            for field in (
                "activity",
                "event_type",
                "place",
                "objects",
                "clothing",
                "facts",
                "ocr_text",
                "source_text",
                "confidence",
            )
        },
    )


def apply_caption(identifier, before, caption, event_ids, repos):
    from sentrix.application.retrieval.index_observation_text import IndexObservationText

    directory = OUTPUT / "captions" / identifier
    current = repair.snapshot("observations", before["id"])
    if (directory / "success.json").exists():
        assert current == json.loads((directory / "success.json").read_text())["after"]
        return {"kind": "caption", "id": identifier, "status": "reused"}
    if current == before:
        original = repos.observations.get(before["id"])
        observation = repos.observations.enrich(original.id, semantics=caption_semantics(original, caption))
        save(directory / "applied.json", {"after": repair.snapshot("observations", original.id)})
    else:
        assert (directory / "applied.json").exists(), "Inspect unrecorded target change"
        assert current == json.loads((directory / "applied.json").read_text())["after"]
        observation = repos.observations.get(before["id"])
    current = repair.snapshot("observations", before["id"])
    changed = {key for key, value in before.items() if current[key] != value}
    assert changed <= {"caption", "revision", "updated_at"}
    assert observation.caption == caption and observation.revision == before["revision"] + 1
    indexes = []
    for event_id in event_ids:
        outcome = IndexObservationText(embedder=repos.embedder, vectors=repos.vectors).index(
            observation,
            event_id,
            force=True,
        )
        assert outcome.indexed and outcome.rows == 3
        indexes.append(asdict(outcome))
    lexical_rows = repos.lexical.refresh_from_observation(observation)
    assert lexical_rows > 0
    save(
        directory / "success.json",
        {
            "after": repair.snapshot("observations", before["id"]),
            "indexes": indexes,
            "lexical_rows": lexical_rows,
            "new_generation_requests": 0,
        },
    )
    return {"kind": "caption", "id": identifier, "status": "completed"}


def protected_fingerprints(connection, plan):
    """Keep compact digests of all records outside this exact write set."""
    observations = [row["id"] for row in plan["caption_before"].values()]
    summaries = [item["event"]["id"] for item in plan["stale_events"]]
    event_ids = sorted(set(summaries + [e for ids in plan["caption_event_ids"].values() for e in ids]))
    obs_marks, event_marks = ",".join("?" for _ in observations), ",".join("?" for _ in event_ids)
    result = {}
    for table in TABLES:
        where, parameters = "1", ()
        if table == "observations":
            where, parameters = f"id NOT IN ({obs_marks})", observations
        elif table == "events":
            where, parameters = f"id NOT IN ({','.join('?' for _ in summaries)})", summaries
        elif table == "text_vectors":
            where = (
                f"NOT ((source_type='observation' AND source_id IN ({obs_marks}) "
                f"AND space IN ('episodic','semantic')) OR (source_type='event' "
                f"AND source_id IN ({event_marks}) AND space='episodic'))"
            )
            parameters = observations + event_ids
        elif table in ("observation_search_terms", "observation_search_fts"):
            where, parameters = f"observation_id NOT IN ({obs_marks})", observations
        columns = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
        order = "id" if "id" in columns else ",".join(columns)
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", parameters):
            encoded = json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def freeze():
    assert not OUTPUT.exists(), "Read existing progress and saved results before continuing"
    selection_path = STAGED / "reviewed-selection.json"
    stale_path = CONTENT / "stale-linked-event-candidates.json"
    approval = json.loads(selection_path.read_text())
    staged = json.loads((STAGED / "intent.json").read_text())
    stale = json.loads(stale_path.read_text())["events"]
    assert len(staged["before"]) == len(approval["captions"]) == 5 and len(stale) == 8
    assert set(approval["captions"]) == set(staged["before"])
    for relative, expected in approval["evidence_sha256"].items():
        assert sha(STAGED / relative) == expected
    for identifier, before in staged["before"].items():
        assert repair.snapshot("observations", before["id"]) == before and not before["caption"].strip()
        assert staged["event_ids"][identifier]
        assert sha(staged["assets"][identifier]["path"]) == staged["source_sha256"][identifier]
    for item in stale:
        assert item["event"]["source_type"] is None
        assert repair.snapshot("events", item["event"]["id"]) == item["event"]
        assert item["observations"]
        for before in item["observations"]:
            assert before["caption"].strip() and repair.snapshot("observations", before["id"]) == before
    plan = {
        "scope_id": SCOPE,
        "created_at": time.time(),
        "caption_before": staged["before"],
        "caption_event_ids": staged["event_ids"],
        "captions": approval["captions"],
        "assets": staged["assets"],
        "source_sha256": staged["source_sha256"],
        "stale_events": stale,
        "selection_sha256": sha(selection_path),
        "stale_sha256": sha(stale_path),
        "model": repair.MODEL,
        "enable_thinking": False,
        "max_tokens": 4096,
        "workers": 4,
    }
    with connect() as connection:
        plan["protected_before"] = protected_fingerprints(connection, plan)
    save(OUTPUT / "plan.json", plan)
    return plan


def summarize(item, repos):
    for observation in item["observations"]:
        assert repair.snapshot("observations", observation["id"]) == observation
    event = item["event"]
    status = repair.repair_summary(event, repos, source_type=None)
    return {"kind": "summary", "id": event["id"], "status": status}


def verify(plan):
    saved = {"captions": [], "summaries": []}
    with connect() as connection:
        assert protected_fingerprints(connection, plan) == plan["protected_before"], (
            "Protected records changed"
        )
        for identifier, before in plan["caption_before"].items():
            success = json.loads((OUTPUT / "captions" / identifier / "success.json").read_text())
            current = repair.snapshot("observations", before["id"])
            assert current == success["after"] and current["caption"] == plan["captions"][identifier]
            assert current["revision"] == before["revision"] + 1
            assert {k for k in current if current[k] != before[k]} <= {"caption", "revision", "updated_at"}
            assert sha(plan["assets"][identifier]["path"]) == plan["source_sha256"][identifier]
            records = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM text_vectors WHERE source_type='observation' AND source_id=?",
                    (before["id"],),
                )
            ]
            assert {r["space"] for r in records} == {"semantic", "episodic"} and len(records) == 2
            assert all(r["dimension"] == 2048 and len(json.loads(r["vector_json"])) == 2048 for r in records)
            for event_id in plan["caption_event_ids"][identifier]:
                projected = dict(
                    connection.execute(
                        "SELECT * FROM text_vectors WHERE source_type='event' "
                        "AND source_id=? AND space='episodic'",
                        (event_id,),
                    ).fetchone()
                )
                assert projected["vector_json"] == records[0]["vector_json"]
                assert json.loads(projected["metadata_json"])["observation_id"] == before["id"]
            assert (
                connection.execute(
                    "SELECT count(*) FROM observation_search_fts WHERE observation_id=?",
                    (before["id"],),
                ).fetchone()[0]
                > 0
            )
            saved["captions"].append(identifier)
        for item in plan["stale_events"]:
            before = item["event"]
            directory = OUTPUT / "summary" / before["id"]
            success = json.loads((directory / "success.json").read_text())
            current = repair.snapshot("events", before["id"])
            assert current == success["after"] and current["revision"] == before["revision"] + 1
            assert {k for k in current if current[k] != before[k]} <= {
                "title",
                "summary",
                "event_type",
                "activity",
                "revision",
                "updated_at",
            }
            assert success["summary_model"] == repair.MODEL and success["text_dimension"] == 2048
            record = dict(
                connection.execute(
                    "SELECT * FROM text_vectors WHERE source_type='event' "
                    "AND source_id=? AND space='episodic'",
                    (before["id"],),
                ).fetchone()
            )
            assert record["dimension"] == len(json.loads(record["vector_json"])) == 2048
            payload = json.loads((directory / "model/accepted-response.json").read_text())
            assert payload["model"] == repair.MODEL and payload["choices"][0]["finish_reason"] == "stop"
            saved["summaries"].append(
                {"id": before["id"], "title": current["title"], "summary": current["summary"]}
            )
    save(OUTPUT / "verification.json", {"status": "storage_indexes_and_protection_verified", **saved})


def run():
    repair.assert_idle()
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    plan = freeze()
    repair.CONTENT = OUTPUT
    repos = repair.repositories()
    progress = {"status": "running", "started_at": time.time(), "results": []}
    try:
        for identifier, before in plan["caption_before"].items():
            progress["results"].append(
                apply_caption(
                    identifier,
                    before,
                    plan["captions"][identifier],
                    plan["caption_event_ids"][identifier],
                    repos,
                )
            )
            save(OUTPUT / "progress.json", progress)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(summarize, item, repos) for item in plan["stale_events"]]
            try:
                for future in as_completed(futures):
                    progress["results"].append(future.result())
                    save(OUTPUT / "progress.json", progress)
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        verify(plan)
        progress["status"] = "completed_pending_fact_review"
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(OUTPUT / "terminal.json", progress)
        print(json.dumps(progress))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "verify"))
    if parser.parse_args().action == "run":
        run()
    else:
        verify(json.loads((OUTPUT / "plan.json").read_text()))
