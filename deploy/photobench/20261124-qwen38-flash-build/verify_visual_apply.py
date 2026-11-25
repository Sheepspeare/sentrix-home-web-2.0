"""Read-only verification of the four reviewed corrections and their derived rows."""

import json

from apply_visual_review import DISPROVEN_ENTITY, REVIEW, SELECTIONS
from inspect_content_recovery import inspect
from recover_videos import connect, save
from verify_video_smoke import differences


def main():
    output = REVIEW / "apply-verification"
    inspect(output / "content.json")
    content = json.loads((output / "content.json").read_text())
    assert content["counts"] == {
        "visual": {"planned": 52, "saved": 52, "matching": 52},
        "summary": {"planned": 955, "saved": 955, "matching": 955},
    }
    assert all(r["file_sha_matches"] and r["text_rows"] and r["lexical_rows"] for r in content["visual"])
    assert all(r["vector_dimensions"] == [2048] for r in content["summary"])
    assert all(not r["missing"] and not r["changed_count"] for r in content["preserved"].values())
    result = {"read_only": True, "counts": content["counts"], "tables": {}, "derived_changes": {}}
    with connect() as connection:
        connection.execute(
            "ATTACH DATABASE ? AS old", ((REVIEW / "apply/before-apply.db").as_uri() + "?mode=ro",)
        )
        targets = []
        for identifier in SELECTIONS:
            success = json.loads((REVIEW / "apply" / identifier / "success.json").read_text())
            canonical = json.loads((REVIEW.parent / "visual" / identifier / "success.json").read_text())
            assert canonical == success
            targets.append(success["after"]["id"])
        for table in (
            "assets",
            "memory_vectors",
            "observations",
            "events",
            "text_vectors",
            "facts",
            "entities",
            "entity_properties",
            "relationships",
        ):
            delta = differences(connection, table)
            delta["added_ids"] = [
                r[0]
                for r in connection.execute(
                    f"SELECT a.id FROM main.{table} a LEFT JOIN old.{table} b ON a.id=b.id WHERE b.id IS NULL"
                )
            ]
            result["tables"][table] = delta
            if table in ("assets", "memory_vectors", "events", "facts"):
                assert delta["missing"] == delta["changed_count"] == len(delta["added_ids"]) == 0
            if table == "observations":
                assert (
                    set(delta["changed_ids"]) == set(targets)
                    and not delta["added_ids"]
                    and not delta["missing"]
                )
            if table in ("entities", "entity_properties", "relationships"):
                ids = delta["changed_ids"] + delta["added_ids"]
                result["derived_changes"][table] = [
                    dict(connection.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone())
                    for identifier in ids
                ]
        vector_ids = (
            result["tables"]["text_vectors"]["changed_ids"] + result["tables"]["text_vectors"]["added_ids"]
        )
        assert not result["tables"]["text_vectors"]["missing"]
        for identifier in vector_ids:
            row = connection.execute("SELECT * FROM text_vectors WHERE id=?", (identifier,)).fetchone()
            assert row["dimension"] == 2048
            if row["source_type"] == "observation":
                assert row["source_id"] in targets
            else:
                assert row["source_type"] == "event"
                oid = json.loads(row["metadata_json"])["observation_id"]
                assert oid in targets
                match = connection.execute(
                    "SELECT vector_json FROM text_vectors WHERE source_type='observation' "
                "AND source_id=? AND space=? AND model_name=?",
                    (oid, row["space"], row["model_name"]),
                ).fetchone()
                assert match is not None and match[0] == row["vector_json"]
        for table in ("entity_observations", "event_entities", "entity_mentions"):
            result["derived_changes"][table] = {
                "removed": [
                    dict(r)
                    for r in connection.execute(
                        f"SELECT * FROM old.{table} EXCEPT SELECT * FROM main.{table}"
                    )
                ],
                "added": [
                    dict(r)
                    for r in connection.execute(
                        f"SELECT * FROM main.{table} EXCEPT SELECT * FROM old.{table}"
                    )
                ],
            }
        entity = connection.execute("SELECT status FROM entities WHERE id=?", (DISPROVEN_ENTITY,)).fetchone()
        assert entity[0] == "rejected"
        assert (
            connection.execute(
                "SELECT count(*) FROM entity_properties WHERE entity_id=? AND status!='rejected'",
                (DISPROVEN_ENTITY,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM relationships WHERE "
                "(subject_entity_id=? OR object_entity_id=?) AND status!='retracted'",
                (DISPROVEN_ENTITY, DISPROVEN_ENTITY),
            ).fetchone()[0]
            == 0
        )
        for table in ("entity_observations", "event_entities"):
            assert (
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE entity_id=?", (DISPROVEN_ENTITY,)
                ).fetchone()[0]
                == 0
            )
    result["status"] = "verified_primary_records_review_derived_deltas"
    save(output / "verification.json", result)
    print(json.dumps({"status": result["status"], "counts": result["counts"], "tables": result["tables"]}))


if __name__ == "__main__":
    main()
