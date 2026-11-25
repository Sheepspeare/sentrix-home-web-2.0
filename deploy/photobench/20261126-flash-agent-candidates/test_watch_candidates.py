import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "candidate_watch", Path(__file__).with_name("watch_candidates.py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_candidate_endpoints_and_checkpoint():
    op = module()
    assert op.PORTS == {"limited": 8878, "large": 8879}
    assert "flash-agent-candidates-20261126" in op.BASE
    assert "flash-dual-baseline-20261126" in op.GATEWAY_LOG
    snapshot = {"status": "running", "phases": {"qa_eval": {"progress": {
        "dispatch_paused": True, "agent_completed": 7,
    }}}}
    assert op.checkpoint(snapshot) is None
    snapshot["phases"]["qa_eval"]["progress"]["agent_completed"] = 8
    assert op.checkpoint(snapshot) == "first_eight_ready"


def test_existing_output_cannot_post_again(tmp_path, monkeypatch):
    op = module()
    (tmp_path / "limited-first-eight").mkdir()
    monkeypatch.setattr(op, "ssh", lambda command: pytest.fail("Another POST is forbidden"))
    with pytest.raises(FileExistsError):
        op.run("limited", tmp_path)


def test_request_alert_distinguishes_configuration_and_rate_limit(monkeypatch):
    op = module()
    monkeypatch.setattr(op.time, "time", lambda: 1000)
    good = {"started_at": 999, "enable_thinking": False, "actual_model": "qwen3.8-flash"}
    assert op.request_alert([good], 990) is None
    assert op.request_alert([{**good, "enable_thinking": True}], 990) == "model_configuration_drift"
    limited = {**good, "http_status": 429}
    assert op.request_alert([limited] * 2, 990) is None
    assert op.request_alert([limited] * 3, 990) == "repeated_429"


@pytest.mark.parametrize("attach", [False, True])
def test_live_launch_is_saved_and_returns_only_at_eight(tmp_path, monkeypatch, attach):
    op = module()
    commands = []
    monkeypatch.setattr(op.time, "sleep", lambda seconds: None)
    payload = tmp_path / "limited-run-payload.json"
    payload.write_text(json.dumps({"sentrix_url": "http://127.0.0.1:8298", "qa_concurrency": 2,
                                   "reasoning": True, "defer_scoring": True}))

    def ssh(command):
        commands.append(command)
        if "POST" in command:
            return '{"run_ids": ["candidate-run"]}'
        if "sha256sum" in command:
            return hashlib.sha256(payload.read_bytes()).hexdigest() + "  run-payload.json"
        if "tail -n" in command:
            return ""
        if command == "df -Pk /home":
            return "Filesystem 1024-blocks Used Available Capacity Mounted on\nx 9 1 3000000 1% /home"
        pytest.fail(command)

    snapshot = {"status": "running", "runtime_snapshot": {"sentrix_url": "http://127.0.0.1:8298"},
                "phases": {"qa_eval": {"progress": {
        "dispatch_paused": True, "agent_completed": 8,
    }}}}
    monkeypatch.setattr(op, "ssh", ssh)
    monkeypatch.setattr(op, "api", lambda port, path: snapshot)
    assert op.run("limited", tmp_path, existing_run="candidate-run" if attach else "") == 0
    posts = [c for c in commands if "POST" in c]
    assert len(posts) == (0 if attach else 1)
    if posts:
        assert "8878" in posts[0] and op.BASE in posts[0]
    assert (tmp_path / "limited-first-eight/launch.json").exists()


def test_baseline_product_endpoint_is_rejected_before_network(tmp_path, monkeypatch):
    op = module()
    payload = {"sentrix_url": "http://127.0.0.1:8296", "qa_concurrency": 2,
               "reasoning": True, "defer_scoring": True}
    (tmp_path / "limited-run-payload.json").write_text(json.dumps(payload))
    monkeypatch.setattr(op, "ssh", lambda command: pytest.fail("Wrong endpoint must not be contacted"))
    with pytest.raises(AssertionError):
        op.run("limited", tmp_path)
