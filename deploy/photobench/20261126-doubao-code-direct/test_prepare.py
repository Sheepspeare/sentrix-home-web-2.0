import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("direct_prepare", Path(__file__).with_name("prepare.py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize(
    "group,ports", [("limited", (8301, 8881, 20, 30)), ("large", (8302, 8882, 100, 600))]
)
def test_frozen_agent_configuration_direct_cloud_and_bounded_subset(tmp_path, monkeypatch, group, ports):
    op = module()
    base = tmp_path / "experiment"
    (base / group).mkdir(parents=True)
    baseline = tmp_path / "existing"
    baseline.mkdir()
    for role in ("api", "eval"):
        (baseline / f"{group}-{role}.env").write_text('SENTRIX_LLM_CONFIG="old-config"\n')
    payload = {"qa_priority_ids": [str(i) for i in range(83)], "reasoning": True, "defer_scoring": True}
    (base / "payload-template.json").write_text(json.dumps(payload))
    monkeypatch.setattr(op, "BASE", base)
    monkeypatch.setattr(op, "BASELINE", baseline)
    units = []
    monkeypatch.setattr(op, "unit", lambda *args: units.append(args))
    op.configure(group, ports, tmp_path / "data", "https://cloud.example/v1", "old-agent")
    api_env = units[0][1]
    assert api_env["SENTRIX_LLM_PROFILE"] == "local"
    assert "SENTRIX_LLM_CONFIG" not in api_env
    assert api_env["SENTRIX_AGENT_TURN_MAX_WORKERS"] == 16
    assert api_env["SENTRIX_VLM_CONTEXT_TOKENS"] == 16384
    assert api_env["SENTRIX_REASONING_MAX_MODEL_CALLS"] == ports[2]
    assert api_env["SENTRIX_REASONING_MAX_TOOL_CALLS"] == ports[3]
    assert api_env["SENTRIX_VLM_BASE_URL"] == "https://cloud.example/v1"
    run = json.loads((base / group / "run-payload.json").read_text())
    control = json.loads((base / group / "dispatch-control.json").read_text())
    assert run["qa_priority_ids"] == [str(i) for i in range(16)]
    assert run["qa_concurrency"] == 16
    assert control == {"concurrency": 16, "pause_after_completed": 16}
    assert run["existing_scope_id"] == op.SCOPE
