import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("visual_smoke", Path(__file__).with_name("smoke.py"))
    op = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(op)
    return op


def test_export_and_check_use_exact_asset_bytes_without_mutating_database(tmp_path):
    op = module()
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"original bytes")
    db_path = tmp_path / "sentrix.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            "CREATE TABLE assets(id TEXT,path TEXT); CREATE TABLE observations(asset_id TEXT,ocr_text TEXT);"
        )
        db.execute("INSERT INTO assets VALUES(?,?)", ("asset", str(image)))
        db.execute("INSERT INTO observations VALUES(?,?)", ("asset", "seat12A"))
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"qa_id": "q", "asset_id": "asset", "question": "seat?"}]))
    before = op.sha(db_path)
    output = tmp_path / "manifest.json"
    op.export_sources(cases, db_path, output)
    assert op.sha(db_path) == before
    manifest = json.loads(output.read_text())
    assert manifest["cases"][0]["ocr"] == "seat12A"
    op.verify_sources(manifest, tmp_path)
    image.write_bytes(b"wrong image")
    with pytest.raises(AssertionError):
        op.verify_sources(manifest, tmp_path)
    with pytest.raises(AssertionError):
        op.export_sources(cases, db_path, output)
