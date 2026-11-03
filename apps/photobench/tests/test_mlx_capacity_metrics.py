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


def test_pending_prefill_queue_supersedes_scheduler_only_gauge_without_double_counting():
    summary = capacity.summarize_resources(
        [
            {
                "engine": {
                    "sentrix_mlx_num_requests_waiting": 7,
                    "vllm_mlx_scheduler_waiting_requests": 2,
                }
            }
        ]
    )
    assert summary["engine_num_requests_waiting_peak"] == 7


def test_tool_internal_unknown_timeout_is_counted_even_after_agent_returns_an_answer():
    item = {
        "tool_trace": [
            {
                "result": {
                    "model_call_observations": [
                        {
                            "diagnostics": {"request_sent": None, "error_category": "request_timeout"},
                            "error_type": "ReadTimeout",
                        }
                    ]
                }
            }
        ]
    }
    counts = capacity.diagnostic_failure_categories([item])
    assert counts["timeout"] == counts["unknown"] == 1


def test_engine_journal_reconciles_main_calls_and_cached_tool_observations():
    vision = {"call_id": "vision", "status": "complete", "prompt_tokens": 900, "completion_tokens": 20}
    items = [
        {
            "model_call_metrics": [
                {"call_id": "agent", "status": "complete", "prompt_tokens": 500, "completion_tokens": 30}
            ],
            "tool_trace": [{"result": {"model_call_observations": [vision]}}] * 2,
        }
    ]
    terminal = [
        {"request_id": "r1", "prompt_tokens": 900, "completion_tokens": 20},
        {"request_id": "r2", "prompt_tokens": 500, "completion_tokens": 30},
    ]
    evidence = capacity.reconcile_model_calls(items, terminal)
    assert evidence["verified"] is True
    assert evidence["expected_calls"] == evidence["recorded_calls"] == 2
    assert capacity.reconcile_model_calls(items, terminal[:1])["verified"] is False
    terminal[1]["request_id"] = "r1"
    assert capacity.reconcile_model_calls(items, terminal)["verified"] is False


def test_incomplete_or_conflicting_application_observations_never_pass_reconciliation():
    call = {"call_id": "c", "status": "complete", "prompt_tokens": 50, "completion_tokens": 3}
    terminal = [{"request_id": "r", "prompt_tokens": 50, "completion_tokens": 3}]
    assert (
        capacity.reconcile_model_calls([{"model_call_metrics": [{**call, "status": "error"}]}], terminal)[
            "verified"
        ]
        is False
    )
    evidence = capacity.reconcile_model_calls(
        [{"model_call_metrics": [call, {**call, "completion_tokens": 4}]}], terminal
    )
    assert evidence["verified"] is False
    assert evidence["conflicting_observations"] == 1


def test_ambiguous_submission_is_never_replayed(tmp_path):
    import httpx
    import pytest

    attempts = []

    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("accepted response lost", request=request)

    receipt = tmp_path / "submission.json"
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(httpx.ReadTimeout):
            capacity.submit_run_once(client, receipt, {"models": ["__current__"]})
        with pytest.raises(FileExistsError):
            capacity.submit_run_once(client, receipt, {"models": ["__current__"]})
    assert len(attempts) == 1
    assert "submitting" in receipt.read_text()


def test_accepted_submission_keeps_exact_receipt(tmp_path):
    import json

    import httpx

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(202, json={"run_ids": ["r1"]}))
    ) as client:
        receipt = tmp_path / "submission.json"
        assert capacity.submit_run_once(client, receipt, {"qa_concurrency": 16}) == "r1"
    stored = json.loads(receipt.read_text())
    assert stored["status"] == "accepted"
    assert stored["response"]["run_ids"] == ["r1"]
    assert stored["payload"] == {"qa_concurrency": 16}


def test_failure_categories_use_diagnostics_not_question_or_answer_prose():
    items = [
        {"answer": "The example says out of memory and timeout", "error": ""},
        {
            "model_call_metrics": [
                {
                    "error_type": "RequestOutcomeUnknownError",
                    "diagnostics": {"error_category": "request_timeout"},
                }
            ]
        },
        {"runtime_turns": [{"error": "database is locked"}, {"error": "database is locked"}]},
        {"model_call_metrics": [{"diagnostics": {"error_category": "output_truncated"}}]},
    ]
    assert capacity.diagnostic_failure_categories(items) == {
        "oom": 0,
        "lock_conflict": 1,
        "service_exit": 0,
        "timeout": 1,
        "unknown": 1,
        "truncation": 1,
    }
