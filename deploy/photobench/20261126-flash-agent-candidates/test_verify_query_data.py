import importlib.util
import sqlite3
from pathlib import Path


def test_identity_coverage_reads_counts_without_importing_runtime_or_changing_database(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "query_data", Path(__file__).with_name("verify_query_data.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "data.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE entities(scope_id, entity_type, status, family_role)")
        db.execute("INSERT INTO entities VALUES('home','person','confirmed','自己')")
        db.execute("CREATE TABLE media(captured_by)")
    before = path.read_bytes()
    receipt = module.inspect(path, "home")
    assert receipt["identity_counts"] == {"confirmed_people": 1, "confirmed_self_anchors": 1}
    assert receipt["photographer_columns"] == {"media": ["captured_by"]}
    assert receipt["mode"] == "ro" and receipt["model_requests"] == 0
    assert path.read_bytes() == before
