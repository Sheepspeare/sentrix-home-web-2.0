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


@pytest.mark.parametrize("status,exit_code", [("completed", 0), ("failed", 1)])
def test_terminal_monitor_continues_past_priority_without_querying_items(
    tmp_path, monkeypatch, status, exit_code
):
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location(
        "terminal_watcher", Path(__file__).with_name("watch_progress.py")
    )
    watcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher)
    (tmp_path / "limited-run-payload.json").write_text(json.dumps({"qa_priority_ids": ["q1"]}))
    snapshots = iter(["running", status])
    calls = []

    def api(port, path):
        assert port == 8876 and path == "runs/existing?include_history=false"
        calls.append(path)
        return {"status": next(snapshots), "phases": {"qa_eval": {"progress": {"agent_completed": 84}}}}

    monkeypatch.setattr(watcher, "api", api)
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        watcher, "ssh", lambda command: (
            "" if command.startswith("tail") else "Filesystem\n/dev/disk 10000000 1 9999999 1% /home"
        )
    )
    assert watcher.watch(
        "limited", "existing", tmp_path, tmp_path / "out", checkpoint="terminal"
    ) == exit_code
    assert len(calls) == 2
    terminal = json.loads((tmp_path / "out/terminal.json").read_text())
    assert terminal["reason"] == "run_terminal"
    assert terminal["worker_stopped_by_watcher"] is False
