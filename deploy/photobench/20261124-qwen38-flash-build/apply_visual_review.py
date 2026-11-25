"""Apply reviewed source excerpts and remove disproven derived content in four records."""

import json
import sqlite3
from dataclasses import asdict

import repair_content as repair
from recover_videos import DATA, connect, save, sha

REVIEW = repair.CONTENT / "visual-review-v2"
SELECTIONS = {
    "asset_78a95b5d74d8": [("header", (3, 4))],
    "asset_816c96df9415": [],
    "asset_abd37b0e8d4c": [("notice", (0, 1, 2, 3, 5))],
    "asset_75deb3249002": [("poster_header", (0, 2, 4))],
}
DISPROVEN_ENTITY = "entity_3b03bd7b0e1e"
STREET = "asset_816c96df9415"


def response_payload(identifier, region):
    directory = REVIEW / identifier / region / "model" / "attempt-001"
    request = json.loads((directory / "request.json").read_text())
    raw = json.loads((directory / "response.json").read_text())
    assert raw["status_code"] == 200
    response = json.loads(raw["body"])
    assert request["model"] == response["model"] == repair.MODEL
    assert request["enable_thinking"] is False and request["max_tokens"] == 4096
    choice = response["choices"][0]
    assert choice["finish_reason"] == "stop" and not choice["message"].get("reasoning_content")
    return json.loads(choice["message"]["content"])


def reviewed_ocr(identifier):
    lines = []
    for region, indices in SELECTIONS[identifier]:
        payload = response_payload(identifier, region)
        source = payload if isinstance(payload, list) else payload["lines"]
        lines.extend(source[i] for i in indices)
    assert all(isinstance(line, str) and "\n" not in line for line in lines)
    assert sum(map(len, lines)) <= 180
    return "\n".join(lines)


def corrected_semantics(original, identifier):
    from sentrix.domain.media.ports import ObservationSemantics

    values = {
        field: getattr(original, field)
        for field in (
            "caption",
            "activity",
            "event_type",
            "place",
            "objects",
            "clothing",
            "facts",
            "source_text",
            "confidence",
        )
    }
    if identifier in ("asset_78a95b5d74d8", STREET):
        description = response_payload(identifier, "description")
        caption = description["caption"]
        if identifier == STREET:
            assert "两名顾客在旁等候，" in caption
            caption = caption.replace("两名顾客在旁等候，", "")
            values["objects"] = tuple(value for value in original.objects if value != "南瓜")
            values["facts"] = tuple(
                fact for fact in original.facts if fact.object not in ("南瓜", "冰糖甜品")
            )
        else:
            assert "，地面可见部分杂物" in caption
            caption = caption.replace("，地面可见部分杂物", "")
            values["objects"] = tuple(value for value in description["objects"] if value != "杂物")
            values["clothing"] = tuple(description["clothing"])
        values.update(caption=caption, activity=description["activity"])
    return ObservationSemantics(**values, ocr_text=reviewed_ocr(identifier))


def reject_disproven_entity(repos, before):
    """Retain the disproven entity as rejected and detach its sole erroneous evidence."""
    from sentrix.infrastructure.persistence.database import Database

    oid = before["id"]
    database = Database(DATA / "sentrix.db", DATA / "unused-migrations")
    with database.connect() as connection, connection:
        entity = dict(connection.execute("SELECT * FROM entities WHERE id=?", (DISPROVEN_ENTITY,)).fetchone())
        assert entity["canonical_name"] == "南瓜" and entity["scope_id"] == repair.SCOPE
        assert entity["status"] == "pending"
        links = [
            dict(r)
            for r in connection.execute(
                "SELECT * FROM entity_observations WHERE entity_id=?", (DISPROVEN_ENTITY,)
            )
        ]
        assert len(links) == 1 and links[0]["observation_id"] == oid
        archived = {"entity": entity, "entity_observations": links}
        for table in ("entity_properties", "relationships", "event_entities"):
            where = "subject_entity_id=? OR object_entity_id=?" if table == "relationships" else "entity_id=?"
            parameters = (
                (DISPROVEN_ENTITY, DISPROVEN_ENTITY) if table == "relationships" else (DISPROVEN_ENTITY,)
            )
            rows = [dict(r) for r in connection.execute(f"SELECT * FROM {table} WHERE {where}", parameters)]
            assert rows and all(
                set(json.loads(row["evidence_ids_json"])) == {oid, "evt_af342e35ac3e"} for row in rows
            )
            archived[table] = rows
        save(REVIEW / "apply" / "disproven-entity-before.json", archived)
        connection.execute(
            "UPDATE entities SET status='rejected',updated_at=datetime('now') WHERE id=?", (DISPROVEN_ENTITY,)
        )
        connection.execute(
            "UPDATE entity_properties SET status='rejected',updated_at=datetime('now') WHERE entity_id=?",
            (DISPROVEN_ENTITY,),
        )
        connection.execute(
            "DELETE FROM entity_observations WHERE entity_id=? AND observation_id=?", (DISPROVEN_ENTITY, oid)
        )
        connection.execute(
            "DELETE FROM event_entities WHERE entity_id=? AND event_id=?",
            (DISPROVEN_ENTITY, "evt_af342e35ac3e"),
        )
    for relationship in archived["relationships"]:
        updated = repos.relationships.retract(relationship["id"])
        assert updated is not None and updated.status == "retracted"


def main():
    from sentrix.application.media.maintain_entities import MaintainObservationEntities
    from sentrix.application.retrieval.index_observation_text import IndexObservationText

    repair.assert_idle()
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    output = REVIEW / "apply"
    assert not output.exists(), "Inspect existing correction before resuming individual stages"
    intent = json.loads((REVIEW / "intent.json").read_text())
    approval = json.loads((REVIEW / "reviewed-selection.json").read_text())
    assert approval["selections"] == {
        key: [[region, list(indices)] for region, indices in value] for key, value in SELECTIONS.items()
    }
    for relative, expected in approval["evidence_sha256"].items():
        assert sha(REVIEW / relative) == expected
    plan = json.loads((repair.CONTENT / "plan.json").read_text())
    assert sha(repair.CONTENT / "plan.json") == intent["plan_sha256"]
    targets = {t["asset_id"]: t for t in plan["visual_targets"] if t["asset_id"] in SELECTIONS}
    assert set(targets) == set(SELECTIONS)
    for identifier, target in targets.items():
        assert repair.snapshot("observations", target["before"]["id"]) == intent["before"][identifier]
    output.mkdir()
    with connect() as source, sqlite3.connect(output / "before-apply.db") as backup:
        source.backup(backup)
    repos = repair.repositories()
    results = []
    for identifier, target in targets.items():
        directory = output / identifier
        old_success = repair.CONTENT / "visual" / identifier / "success.json"
        if old_success.exists():
            save(directory / "before-success.json", json.loads(old_success.read_text()))
        asset = repos.assets.get(identifier)
        assert asset is not None and sha(asset.path) == target["sha256"]
        original = repos.observations.get(target["before"]["id"])
        semantics = corrected_semantics(original, identifier)
        save(directory / "prepared.json", asdict(semantics))
        observation = repos.observations.enrich(original.id, semantics=semantics)
        event_id = target["event_ids"][0]
        index = IndexObservationText(embedder=repos.embedder, vectors=repos.vectors).index(
            observation, event_id, force=True
        )
        assert index.indexed and index.rows > 0
        if identifier == "asset_78a95b5d74d8":
            MaintainObservationEntities(
                entities=repos.entities,
                events=repos.participants,
                assets=repos.assets,
                relationships=repos.relationships,
            ).execute(observation, event_id=event_id)
        if identifier == STREET:
            reject_disproven_entity(repos, intent["before"][identifier])
        lexical = repos.lexical.refresh_from_observation(observation)
        assert lexical > 0
        result = {
            "after": repair.snapshot("observations", original.id),
            "text_index": asdict(index),
            "lexical_rows": lexical,
            "event_ids": target["event_ids"],
            "source_sha256": sha(asset.path),
            "review_evidence": str(REVIEW),
            "ocr_status": "partial_verified_excerpts"
            if semantics.ocr_text
            else "unrecovered_unsupported_text_removed",
            "previous_success_preserved": str(directory / "before-success.json")
            if old_success.exists()
            else None,
            "strategy": "reviewed_model_excerpts_no_human_transcription",
        }
        save(directory / "success.json", result)
        save(old_success, result)
        results.append(identifier)
        save(output / "progress.json", {"completed": results})
    save(
        output / "terminal.json",
        {
            "status": "completed_local_corrections_partial_ocr",
            "completed": results,
            "new_generation_requests": 0,
            "whole_image_ocr_recovered": False,
        },
    )
    print(json.dumps({"status": "completed_local_corrections_partial_ocr", "completed": results}))


if __name__ == "__main__":
    main()
