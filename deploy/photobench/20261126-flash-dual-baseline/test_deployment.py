import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_clone_fingerprint_normalizes_only_asset_path():
    op = module("operate")
    source = sqlite3.connect(":memory:")
    clone = sqlite3.connect(":memory:")
    for db in (source, clone):
        db.execute("create table assets (id text primary key, path text, title text)")
    source.execute("insert into assets values (?,?,?)", ("a", op.ORIGINAL_DATA + "/media/a", "original"))
    clone.execute("insert into assets values (?,?,?)", ("a", "/clone/media/a", "original"))
    expected = op.memory_fingerprints(source, ["assets"], op.ORIGINAL_DATA)
    assert op.memory_fingerprints(clone, ["assets"], "/clone") == expected
    clone.execute("update assets set title='changed'")
    assert op.memory_fingerprints(clone, ["assets"], "/clone") != expected
    clone.execute("update assets set path='/wrong/a'")
    with pytest.raises(AssertionError):
        op.memory_fingerprints(clone, ["assets"], "/clone")


def test_gateway_reports_capacity_without_making_model_request(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "model": "qwen3.8-flash",
                "url": "https://example.test/v1",
                "context_tokens": 131072,
                "max_concurrency": 8,
            }
        )
    )
    monkeypatch.setenv("FLASH_GATEWAY_CONFIG", str(config))
    monkeypatch.setenv("FLASH_REQUEST_LOG", str(tmp_path / "requests.jsonl"))
    monkeypatch.setenv("QWEN_API_KEY", "test-only")
    with TestClient(module("gateway").create_app()) as client:
        assert client.get("/health").json()["max_concurrency"] == 8
        assert client.get("/props").json()["total_slots"] == 8
        assert client.get("/health").json()["enable_thinking"] is False
        assert client.post("/v1/chat/completions", json={"model": "wrong"}).status_code == 400
    assert not (tmp_path / "requests.jsonl").exists()
