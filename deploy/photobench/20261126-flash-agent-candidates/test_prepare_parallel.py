import importlib.util
from pathlib import Path


def module():
    spec = importlib.util.spec_from_file_location(
        "parallel_prepare", Path(__file__).with_name("prepare_parallel.py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_parallel_configuration_uses_distinct_units_ports_and_directory():
    op = module()
    release = Path(__file__).parents[3]
    p = op.candidate_module(release)
    assert p.GROUPS == {"large": (8300, 8880, 100, 600)}
    assert p.unit_name("large-api") == "sentrix-flash-query-v7-large-api.service"
    assert p.BASE != p.BASELINE
    assert (
        Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-agent-candidates-20261126") != p.BASE
    )


def test_parallel_payload_changes_only_transport_and_control():
    op = module()
    original = {
        "sentrix_url": "http://127.0.0.1:8299",
        "qa_concurrency_control_path": "/old",
        "qa_priority_ids": ["a", "b"],
        "defer_scoring": True,
    }
    result = op.payload_for_parallel(original)
    assert result["sentrix_url"] == "http://127.0.0.1:8300"
    assert result["qa_concurrency_control_path"] == str(op.BASE / "large/dispatch-control.json")
    assert {k: v for k, v in result.items() if k not in {"sentrix_url", "qa_concurrency_control_path"}} == {
        k: v for k, v in original.items() if k not in {"sentrix_url", "qa_concurrency_control_path"}
    }
    assert original["sentrix_url"] == "http://127.0.0.1:8299"
