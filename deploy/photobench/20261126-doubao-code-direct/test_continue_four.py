import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("continue_four", Path(__file__).with_name("continue_four.py"))
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)


def test_missing_excludes_original_and_supplement_without_changing_order():
    snapshot = {"rows": [{"qa_id": f"q{i}"} for i in range(1013)]}
    present = {"q0", "q3", "q1000"}
    rows = op.select_missing(snapshot, present)
    assert len(rows) == 1010
    assert [r["qa_id"] for r in rows[:3]] == ["q1", "q2", "q4"]
    rows[0]["qa_id"] = "changed"
    assert snapshot["rows"][1]["qa_id"] == "q1"


def test_foreign_answer_is_rejected():
    import pytest

    with pytest.raises(AssertionError):
        op.select_missing({"rows": [{"qa_id": f"q{i}"} for i in range(1013)]}, {"foreign"})
