import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("group,port", [("limited", 8878), ("large", 8879)])
@pytest.mark.parametrize("changed", [False, True])
def test_cancelled_placeholder_rows_do_not_replace_answer_preservation_check(
    tmp_path, monkeypatch, changed, group, port
):
    spec = importlib.util.spec_from_file_location(
        "revise_limited", Path(__file__).with_name("revise_limited.py")
    )
    op = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(op)
    accepted = {f"qa-{n}": hashlib.sha256(f"answer-{n}".encode()).hexdigest() for n in range(8)}
    (tmp_path / f"{group}-v1-accepted-eight.json").write_text(json.dumps(accepted))
    monkeypatch.setattr(op, "BASE", tmp_path)

    def api(actual_port, path):
        assert actual_port == port
        if "search=" in path:
            qa_id = path.split("search=")[1].split("&")[0]
            return {"items": [{"qa_id": qa_id, "index": int(qa_id[3:]) + 100}]}
        if "/items/" in path:
            index = int(path.rsplit("/", 1)[1]) - 100
            answer = "changed" if changed and index == 0 else f"answer-{index}"
            return {"item": {"answer": answer}}
        return {
            "status": "cancelled",
            "item_count": 1013,
            "phases": {"qa_eval": {"progress": {"agent_completed": 8}}},
        }

    monkeypatch.setattr(op.h, "api", api)
    if changed:
        with pytest.raises(AssertionError):
            op.verify_archived_answers(tmp_path, group=group)
        assert not (tmp_path / "answer-preservation.json").exists()
    else:
        op.verify_archived_answers(tmp_path, group=group)
        assert json.loads((tmp_path / "answer-preservation.json").read_text())["verified_answers"] == 8
