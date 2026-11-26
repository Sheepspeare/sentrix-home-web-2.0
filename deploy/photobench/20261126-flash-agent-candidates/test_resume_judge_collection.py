import importlib.util
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "resume", Path(__file__).with_name("resume_judge_collection.py")
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_uuid_is_resolved_after_a_live_index_moves():
    replies = iter(
        [
            {"items": [{"qa_id": "wanted", "index": 3}]},
            {"item": {"qa_id": "different", "index": 3}},
            {"items": [{"qa_id": "wanted", "index": 4}]},
            {"item": {"qa_id": "wanted", "index": 4}},
        ]
    )
    calls = []

    def get(port, path):
        calls.append(path)
        return next(replies)

    item, moved = module().detail_by_qa(get, 1, "run", "wanted")
    assert item["qa_id"] == "wanted" and len(moved) == 1
    assert calls[0] == calls[2] == "runs/run/items?search=wanted&page=1&page_size=10"


def test_persistently_moving_indices_stop_without_returning_another_qa():
    def get(port, path):
        if "?" in path:
            return {"items": [{"qa_id": "wanted", "index": 1}]}
        return {"item": {"qa_id": "different", "index": 1}}

    with pytest.raises(AssertionError, match="kept moving"):
        module().detail_by_qa(get, 1, "run", "wanted")


def test_uuid_lookup_requires_unique_identity():
    with pytest.raises(AssertionError, match="not unique"):
        module().detail_by_qa(lambda *args: {"items": []}, 1, "run", "wanted")
