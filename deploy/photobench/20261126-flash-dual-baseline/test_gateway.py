import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("flash_gateway", Path(__file__).with_name("gateway.py"))
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@pytest.mark.parametrize(
    "body,config,enabled,source",
    [
        ({}, {}, False, "service_default"),
        ({}, {"enable_thinking": True}, True, "service_default"),
        ({"enable_thinking": False}, {"enable_thinking": True}, False, "request.enable_thinking"),
        ({"enable_thinking": True}, {"enable_thinking": False}, True, "request.enable_thinking"),
        ({"chat_template_kwargs": {"enable_thinking": True}}, {}, True, "request.chat_template_kwargs"),
        ({"thinking": {"type": "enabled"}}, {}, True, "request.thinking"),
        (
            {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": True}},
            {},
            False,
            "request.enable_thinking",
        ),
    ],
)
def test_request_thinking_precedence(body, config, enabled, source):
    original = body.copy()
    value, actual_source = gateway.normalize_thinking(body, config)
    assert body == original
    assert value["enable_thinking"] is enabled and actual_source == source
    assert "chat_template_kwargs" not in value and "thinking" not in value


def test_budget_and_other_generation_settings_survive():
    value, _ = gateway.normalize_thinking(
        {"thinking": {"type": "enabled", "budget_tokens": 8192}, "max_tokens": 4096, "temperature": 0}, {}
    )
    assert value == {
        "enable_thinking": True,
        "thinking_budget": 8192,
        "max_completion_tokens": 4096,
        "temperature": 0,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"enable_thinking": "false"},
        {"thinking": {"type": "high"}},
        {"enable_thinking": True, "thinking_budget": -1},
    ],
)
def test_invalid_request_is_rejected(body):
    with pytest.raises(ValueError):
        gateway.normalize_thinking(body, {})
