import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("candidate_prepare", Path(__file__).with_name("prepare.py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_units_use_candidate_release_and_names(tmp_path, monkeypatch):
    op = module()
    base = tmp_path / "candidate"
    base.mkdir()
    home = tmp_path / "home"
    (home / ".config/systemd/user").mkdir(parents=True)
    monkeypatch.setattr(op, "BASE", base)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    op.unit("limited-api", {"SENTRIX_DATA_DIR": base / "limited/data"}, 8298, "sentrix.main", "/src", True)
    unit = home / ".config/systemd/user/sentrix-flash-candidates-limited-api.service"
    content = unit.read_text()
    assert f"WorkingDirectory={base}/release" in content
    assert "--port 8298" in content
    assert "sentrix-flash-dual" not in content
    assert "gateway" not in content
    assert f"EnvironmentFile={op.helper.OLD}/secrets.env" in content
    assert unit.stat().st_mode & 0o777 == 0o600
    with pytest.raises(AssertionError):
        op.unit("limited-api", {}, 8298, "sentrix.main", "/src")


@pytest.mark.parametrize(
    "group,budgets,product,evaluation", [("limited", (20, 30), 8298, 8878), ("large", (100, 600), 8299, 8879)]
)
def test_group_configuration_keeps_shared_gateway_and_two_workers(
    tmp_path, monkeypatch, group, budgets, product, evaluation
):
    op = module()
    base = tmp_path / "candidate"
    target = base / group
    target.mkdir(parents=True)
    baseline = tmp_path / "baseline"
    old = baseline / group
    (old / "config").mkdir(parents=True)
    original = {
        "product_settings": {
            "unrelated": 7,
            "reasoning_max_model_calls": budgets[0],
            "reasoning_max_tool_calls": budgets[1],
            "reasoning_max_search_calls": 100,
            "reasoning_max_other_tool_calls": 500,
            "agent_turn_workers": 4,
        },
        "model_base_url": op.helper.GATEWAY,
    }
    (old / "config/runtime_connection.json").write_text(json.dumps(original))
    op.helper.env(
        baseline / f"{group}-api.env",
        {
            "SENTRIX_KEEP": "value",
            "SENTRIX_VLM_BASE_URL": op.helper.GATEWAY,
            "SENTRIX_REASONING_MAX_MODEL_CALLS": budgets[0],
            "SENTRIX_REASONING_MAX_TOOL_CALLS": budgets[1],
        },
    )
    op.helper.env(baseline / f"{group}-eval.env", {"BENCH_KEEP": "value"})
    monkeypatch.setattr(op, "BASE", base)
    monkeypatch.setattr(op, "BASELINE", baseline)
    units = []
    monkeypatch.setattr(op, "unit", lambda *args: units.append(args))
    data = target / "data"
    op.configure_group(group, op.GROUPS[group], "candidate-sha", data)
    connection = json.loads((target / "config/runtime_connection.json").read_text())
    settings = connection["product_settings"]
    assert (settings["reasoning_max_model_calls"], settings["reasoning_max_tool_calls"]) == budgets
    assert settings["agent_turn_workers"] == 2
    assert settings["reasoning_max_search_calls"] == 100
    assert settings["reasoning_max_other_tool_calls"] == 500
    assert settings["unrelated"] == 7
    assert json.loads((old / "config/runtime_connection.json").read_text()) == original
    assert connection["sentrix_url"] == f"http://127.0.0.1:{product}"
    assert connection["model_base_url"] == "http://127.0.0.1:18148/v1"
    assert json.loads((target / "dispatch-control.json").read_text()) == {"concurrency": 2}
    api, benchmark = units
    assert api[0] == f"{group}-api" and api[2] == product
    assert api[1]["SENTRIX_DATA_DIR"] == data
    assert api[1]["SENTRIX_AGENT_TURN_MAX_WORKERS"] == 2
    assert api[1]["SENTRIX_KEEP"] == "value"
    assert benchmark[2] == evaluation
    assert benchmark[1]["PHOTOBENCH_RESULTS_ROOT"] == target / "results"
    assert benchmark[1]["PHOTOBENCH_SENTRIX_SERVICE_UNIT"] == f"sentrix-flash-candidates-{group}-api.service"
    assert benchmark[1]["BENCH_KEEP"] == "value"


def test_existing_intent_stops_before_any_operations(tmp_path, monkeypatch):
    op = module()
    (tmp_path / "prepare-intent.json").write_text("{}")
    monkeypatch.setattr(op, "BASE", tmp_path)
    monkeypatch.setattr(op, "check_ports", lambda: pytest.fail("Must inspect saved outcome first"))
    with pytest.raises(AssertionError, match="do not replay"):
        op.prepare()


def test_port_conflict_stops_prepare(monkeypatch):
    op = module()

    class OccupiedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect_ex(self, address):
            assert address == ("127.0.0.1", 8298)
            return 0

    monkeypatch.setattr(op.socket, "socket", OccupiedSocket)
    with pytest.raises(AssertionError, match="8298 already occupied"):
        op.check_ports()
