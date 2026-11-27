import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "representative", Path(__file__).with_name("representative_qa.py")
)
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)


def test_only_missing_fixed_ids_preserving_snapshot_order():
    ids = [f"q{n}" for n in range(83)]
    snapshot = {"rows": [{"qa_id": f"q{n}"} for n in reversed(range(90))]}
    rows = op.missing_rows(snapshot, ids, {"q0", "q2", "other"})
    assert [v["qa_id"] for v in rows] == [f"q{n}" for n in reversed(range(83)) if n not in {0, 2}]
    assert len(snapshot["rows"]) == 90
