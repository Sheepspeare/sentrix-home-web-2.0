import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "direct_score", Path(__file__).with_name("score_direct_exports.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_expected_direct_cloud_endpoint_is_allowed():
    url = "https://llm-355ba48p9iw7pkjz.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/"
    assert module.endpoint(url) == url.rstrip("/")
    for bad in [
        "http://127.0.0.1:18148/v1",
        "https://example.com/compatible-mode/v1",
        url + "chat/completions",
    ]:
        with pytest.raises(AssertionError):
            module.endpoint(bad)
