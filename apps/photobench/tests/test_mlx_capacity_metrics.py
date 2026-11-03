"""Evidence parsing must preserve histogram buckets and missing observations."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("mlx_capacity", ROOT / "scripts/benchmarks/mlx_capacity.py")
assert spec and spec.loader
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


def test_histogram_bounds_and_outcomes_are_not_summed():
    parsed = capacity.prometheus_values(
        'vllm_mlx_inference_ttft_seconds_bucket{le="1.0"} 2\n'
        'vllm_mlx_inference_ttft_seconds_bucket{le="5.0"} 7\n'
        'vllm_mlx_inference_requests_total{result="error"} 1\n'
        'vllm_mlx_inference_requests_total{result="success"} 6\n'
    )
    assert len(parsed) == 4
    assert parsed['vllm_mlx_inference_ttft_seconds_bucket{le="1.0"}'] == 2
    assert parsed['vllm_mlx_inference_requests_total{result="error"}'] == 1


def test_fallback_engine_gauges_and_missing_values():
    samples = [
        {"engine": {"vllm_mlx_scheduler_running_requests": 3, "vllm_mlx_scheduler_waiting_requests": 7}},
        {"engine_unavailable": "ReadTimeout"},
    ]
    summary = capacity.summarize_resources(samples)
    assert summary["engine_num_requests_running_peak"] == 3
    assert summary["engine_num_requests_waiting_peak"] == 7
    assert summary["engine_metrics_missing_samples"] == 1
    assert summary["gpu_percent"]["max"] is None
    assert summary["swap_in_bytes_delta"] is None
