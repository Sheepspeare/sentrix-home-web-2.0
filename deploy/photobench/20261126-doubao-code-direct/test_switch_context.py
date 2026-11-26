import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("switch_context", Path(__file__).with_name("switch_context.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_context_values_change():
    original = (
        'SENTRIX_VLM_CONTEXT_TOKENS="16384"\n'
        'SENTRIX_VLM_REASONING_CONTEXT_TOKENS="16384"\n'
        'SENTRIX_REASONING_MAX_MODEL_CALLS="20"\n'
    )
    expected = original.replace('"16384"', '"131072"')
    assert module.context_env(original) == expected


def test_drain_requires_both_evaluator_and_product_idle():
    live = {
        "phases": {
            "qa_eval": {
                "agent_executed_qa_ids": ["a", "b"],
                "progress": {"dispatch_paused": True, "agent_completed": 1},
            }
        }
    }
    assert not module.drained(live, 0)
    live["phases"]["qa_eval"]["progress"]["agent_completed"] = 2
    assert not module.drained(live, 1)
    assert module.drained(live, 0)
    live["phases"]["qa_eval"]["progress"]["dispatch_paused"] = False
    assert not module.drained(live, 0)


def test_wrong_initial_context_is_rejected():
    import pytest

    with pytest.raises(AssertionError):
        module.context_env(
            'SENTRIX_VLM_CONTEXT_TOKENS="32768"\nSENTRIX_VLM_REASONING_CONTEXT_TOKENS="16384"\n'
        )
