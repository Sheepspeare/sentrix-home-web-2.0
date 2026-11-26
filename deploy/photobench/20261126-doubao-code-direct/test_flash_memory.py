import importlib.util
import sqlite3
from pathlib import Path

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_memory_comparison_normalizes_only_asset_path(tmp_path):
    op, new = load("prepare"), load("prepare_flash_memory")
    db = sqlite3.connect(":memory:")
    for table in op.TABLES:
        schema = "id text primary key, path text, value blob" if table == "assets" else "id text primary key"
        db.execute(f'create table "{table}" ({schema})')
    original = new.ORIGINAL_DATA + "/media/example.jpg"
    db.execute("insert into assets values(?,?,?)", ("asset", original, b"value"))
    expected = op.fingerprints(db)
    db.execute("update assets set path=?", (str(tmp_path / "media/example.jpg"),))
    op.raw_fingerprints = op.fingerprints
    assert new.normalized_fingerprints(op, db, tmp_path) == expected
    db.execute("update assets set value=?", (b"changed",))
    assert new.normalized_fingerprints(op, db, tmp_path) != expected
    db.execute("update assets set path='/other/file'")
    with pytest.raises(AssertionError):
        new.normalized_fingerprints(op, db, tmp_path)


@pytest.mark.parametrize("group", ["limited", "large"])
def test_flash_memory_keeps_historical_execution_configuration(tmp_path, monkeypatch, group):
    op, new = load("prepare"), load("prepare_flash_memory")
    monkeypatch.setattr(op, "SCOPE", new.SCOPE)
    helper = load("test_prepare")
    monkeypatch.setattr(helper, "module", lambda: op)
    helper.test_frozen_agent_configuration_direct_cloud_and_bounded_subset(
        tmp_path, monkeypatch, group, new.GROUPS[group]
    )
