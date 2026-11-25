import importlib.util
import sqlite3
from pathlib import Path

import pytest


def test_readonly_verifier_supports_production_time_functions_without_writes(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "verify_pages", Path(__file__).with_name("verify_group_pages.py")
    )
    op = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(op)
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as db:
        db.execute("create table sample (captured_at text)")
        db.execute("insert into sample values ('2023-02-09T12:00:00+08:00')")
    before = database.read_bytes()
    with op.ReadOnlyDatabase(database).connect() as db:
        value = db.execute("select capture_sort_key(captured_at) from sample").fetchone()[0]
        assert value == "1:2023-02-09T04:00:00.000000+00:00"
        assert (
            db.execute(
                "select capture_in_bounds(captured_at, ?, ?) from sample",
                ("0001-01-01T00:00:00", "2023-02-10T00:00:00"),
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("delete from sample")
    assert database.read_bytes() == before
