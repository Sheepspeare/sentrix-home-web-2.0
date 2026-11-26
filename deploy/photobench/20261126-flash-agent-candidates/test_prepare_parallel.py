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
    p = op.candidate_module()
    assert p.GROUPS == {"large": (8300, 8880, 100, 600)}
    assert p.unit_name("large-api") == "sentrix-flash-query-v7-large-api.service"
    assert p.BASE != p.BASELINE
    assert (
        Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-agent-candidates-20261126") != p.BASE
    )


def test_parallel_cache_is_isolated_and_preserves_existing_candidate(tmp_path, monkeypatch):
    op = module()
    p = op.candidate_module()
    assert p.cache_path("large") == Path("/dev/shm/sentrix-flash-query-v7-parallel-20261126-large")
    old = tmp_path / "sentrix-flash-candidates-20261126-large"
    old.mkdir()
    (old / "sentinel").write_text("active-v6")
    cache = tmp_path / p.cache_path("large").name
    data = tmp_path / "parallel/data"
    data.mkdir(parents=True)
    monkeypatch.setattr(p, "cache_path", lambda group: cache)
    calls = []
    monkeypatch.setattr(p.helper, "build_cache", lambda target: calls.append(target))
    p.build_album_cache("large", data)
    assert (data / "qdrant").resolve() == cache
    assert (old / "sentinel").read_text() == "active-v6"
    assert calls == [data]


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


def test_parallel_release_uses_verified_product_and_existing_evaluation_without_editing_either(tmp_path):
    op = module()
    product = tmp_path / "product"
    (product / "apps/api").mkdir(parents=True)
    (product / "build-info.json").write_text('{"revision":"product"}')
    evaluation = tmp_path / "baseline/apps/photobench"
    evaluation.mkdir(parents=True)
    target = tmp_path / "parallel/release"
    target.parent.mkdir()
    op.compose_release(product, evaluation, target)
    assert (target / "apps/api").resolve() == product / "apps/api"
    assert (target / "apps/photobench").resolve() == evaluation
    assert (target / "build-info.json").read_bytes() == (product / "build-info.json").read_bytes()
    assert not (product / "apps/photobench").exists()
