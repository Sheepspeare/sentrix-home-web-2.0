import hashlib
import importlib.util
import json
from pathlib import Path


def test_cancelled_unstarted_rows_are_not_read():
    path = Path(__file__).with_name("switch_fusion_direct.py")
    spec = importlib.util.spec_from_file_location("switch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    def api(port, path):
        calls.append(path)
        if "/items?" in path:
            return {
                "items": [{"index": 0, "qa_id": "unstarted"}, {"index": 4, "qa_id": "saved"}],
                "has_next": False,
            }
        if path.endswith("/items/4"):
            return {"item": {"qa_id": "saved", "trace_storage": {"revision": "r1"}}}
        assert "/items/4/content?" in path
        return {"read_revision": "r1", "text": json.dumps("answer"), "next_offset": None}

    mod.api = api
    assert mod.answer_hashes("run", {"saved"}) == {
        "saved": hashlib.sha256(json.dumps("answer").encode()).hexdigest()
    }
    assert all("/items/0" not in call for call in calls)
