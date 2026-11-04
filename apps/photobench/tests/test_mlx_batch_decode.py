"""Row-routing regressions; real Metal/matcher parity is a separate qualification."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "mlx_batch_decode", ROOT / "scripts/benchmarks/mlx_batch_decode.py"
)
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_mixed_processor_order_survives_reordered_rows(monkeypatch):
    monkeypatch.setattr(adapter.importlib, "import_module", lambda name: np)

    class Constraint:
        def __init__(self, factor):
            self.factor = factor

    batches = []

    def apply(processors, histories, logits):
        batches.append((processors, histories))
        return np.concatenate([row[None] * p.factor for p, row in zip(processors, logits, strict=True)])

    grammar = SimpleNamespace(
        module=SimpleNamespace(LLGuidanceJSONSchemaLogitsProcessor=Constraint), apply=apply
    )
    first, second = Constraint(2), Constraint(3)

    def add(tokens, logits):
        return logits + tokens[-1]

    values = np.array([[1.0], [10.0], [100.0]])
    actual = adapter.process_rows(values, [[add, first], [second, add], None], [[4], [5], [6]], grammar)
    np.testing.assert_array_equal(actual, [[10.0], [35.0], [100.0]])
    assert batches == [([second], [[5]]), ([first], [[4]])]
    actual = adapter.process_rows(values[[1, 0]], [[second, add], [add, first]], [[5], [4]], grammar)
    np.testing.assert_array_equal(actual, [[35.0], [10.0]])


def test_duplicate_matcher_rejected_before_advancing_any_grammar():
    grammar = object.__new__(adapter.GrammarBatch)
    matcher = object()
    processors = [SimpleNamespace(_matcher=matcher), SimpleNamespace(_matcher=matcher)]
    with pytest.raises(ValueError, match="duplicate request grammar"):
        grammar.apply(processors, [[], []], None)


def test_kv_observation_preserves_outputs_and_fallback():
    expected = object()
    module = SimpleNamespace(_qwen3_5_ragged_decode_attention=lambda ok: expected if ok else None)
    before = dict(adapter.COUNTERS)
    adapter.install_kv_observer(module)
    assert module._qwen3_5_ragged_decode_attention(True) is expected
    assert module._qwen3_5_ragged_decode_attention(False) is None
    assert adapter.COUNTERS["strided_kv_calls"] == before["strided_kv_calls"] + 1
    assert adapter.COUNTERS["strided_kv_fallbacks"] == before["strided_kv_fallbacks"] + 1
