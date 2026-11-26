import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "group,port,stage",
    [
        ("limited", 8878, "v2"),
        ("large", 8879, "v2"),
        ("large", 8879, "v3"),
        ("large", 8879, "v4"),
        ("large", 8879, "v5"),
        ("large", 8879, "v6"),
        ("large", 8879, "v7"),
    ],
)
@pytest.mark.parametrize("changed", [False, True])
def test_cancelled_placeholder_rows_do_not_replace_answer_preservation_check(
    tmp_path, monkeypatch, changed, group, port, stage
):
    spec = importlib.util.spec_from_file_location(
        "revise_limited", Path(__file__).with_name("revise_limited.py")
    )
    op = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(op)
    count = op.STAGES[stage]["answer_count"]
    accepted = {f"qa-{n}": hashlib.sha256(f"answer-{n}".encode()).hexdigest() for n in range(count)}
    (
        tmp_path / f"{group}-{op.STAGES[stage]['accepted']}-accepted-{op.STAGES[stage]['answer_word']}.json"
    ).write_text(json.dumps(accepted))
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
            "phases": {"qa_eval": {"progress": {"agent_completed": count}}},
        }

    monkeypatch.setattr(op.h, "api", api)
    if changed:
        with pytest.raises(AssertionError):
            op.verify_archived_answers(tmp_path, group=group, stage=stage)
        assert not (tmp_path / "answer-preservation.json").exists()
    else:
        op.verify_archived_answers(tmp_path, group=group, stage=stage)
        receipt = json.loads((tmp_path / "answer-preservation.json").read_text())
        assert receipt["verified_answers"] == count
        assert receipt["unexecuted_rows"] == 1013 - count
