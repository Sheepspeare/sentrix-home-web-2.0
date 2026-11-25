import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "instance,group,port",
    [
        ("baseline", "limited", 8876),
        ("baseline", "large", 8877),
        ("candidate", "limited", 8878),
        ("candidate", "large", 8879),
    ],
)
def test_existing_run_monitor_uses_selected_instance_without_launching(
    tmp_path, monkeypatch, instance, group, port
):
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location(
        "progress_watcher", Path(__file__).with_name("watch_progress.py")
    )
    watcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher)
    (tmp_path / f"{group}-run-payload.json").write_text(json.dumps({"qa_priority_ids": ["q1"]}))
    calls = []

    def api(actual_port, path):
        assert actual_port == port
        calls.append(path)
        if "/items?" in path:
            return {"items": [{"qa_id": "q1"}], "has_next": False}
        assert path == "runs/existing?include_history=false"
        return {"status": "running", "phases": {"qa_eval": {"progress": {"agent_completed": 1}}}}

    monkeypatch.setattr(watcher, "api", api)
    monkeypatch.setattr(
        watcher,
        "ssh",
        lambda command: (
            "" if command.startswith("tail") else "Filesystem\n/dev/disk 10000000 1 9999999 1% /home"
        ),
    )
    assert watcher.watch(group, "existing", tmp_path, tmp_path / "out", instance=instance) == 0
    assert len(calls) == 2
    intent = json.loads((tmp_path / "out/intent.json").read_text())
    assert intent["evaluation_port"] == port and intent["mutates_run"] is False
