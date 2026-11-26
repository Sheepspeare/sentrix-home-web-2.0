"""Inspect query-field coverage using SQLite read-only mode, without a Container or model."""

import argparse
import json
import sqlite3
from pathlib import Path


def inspect(database, scope):
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = {
            table: [row[1] for row in db.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')]
            for table in tables
        }
        photographer_fields = {
            table: [
                name
                for name in names
                if any(
                    term in name.lower()
                    for term in (
                        "captured_by",
                        "photographer",
                        "creator_person",
                        "camera_owner",
                    )
                )
            ]
            for table, names in columns.items()
        }
        counts = {}
        if {"scope_id", "entity_type", "status", "family_role"} <= set(columns.get("entities", [])):
            counts["confirmed_people"] = db.execute(
                "SELECT count(*) FROM entities WHERE scope_id=? AND entity_type='person' "
                "AND status='confirmed'",
                (scope,),
            ).fetchone()[0]
            counts["confirmed_self_anchors"] = db.execute(
                "SELECT count(*) FROM entities WHERE scope_id=? AND entity_type='person' "
                "AND status='confirmed' AND family_role='自己'",
                (scope,),
            ).fetchone()[0]
        return {
            "database": str(database),
            "scope_id": scope,
            "mode": "ro",
            "model_requests": 0,
            "photographer_columns": {table: names for table, names in photographer_fields.items() if names},
            "identity_counts": counts,
            "coverage_note": "Column coverage only; textual mentions are not an identity index.",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as target:
        json.dump(inspect(args.database, args.scope), target, ensure_ascii=False, indent=2)
