"""The chunked decoder must include its pending token in each request's grammar state."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("serve_mlx", ROOT / "scripts/benchmarks/serve_mlx.py")
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_chunked_context_includes_current_token_without_mutating_request_history():
    requests = [SimpleNamespace(output_tokens=[]), SimpleNamespace(output_tokens=[7])]
    original = [request.output_tokens for request in requests]
    result = adapter.processor_context(requests, original, [[90], [12]])
    assert result == [[90], [7, 12]]
    assert original == [[], [7]]


def test_normal_decoder_context_is_not_duplicated_and_no_processors_stay_absent():
    requests = [SimpleNamespace(output_tokens=[90])]
    complete = [[90, 4754]]
    assert adapter.processor_context(requests, complete, [[4754]]) == complete
    assert adapter.processor_context(requests, None, [[4754]]) is None
