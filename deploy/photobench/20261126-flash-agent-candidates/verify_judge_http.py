"""Verify saved Judge responses against existing gateway archives, without inference."""

import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def verify(config):
    expected = {e["response_id"]: e for e in config["expected"]}
    assert len(expected) == len(config["expected"]), "Duplicate expected response ID"
    found = {}
    raw_ids = set()
    for line in Path(config["log"]).read_text().splitlines():
        record = json.loads(line)
        response_id = record.get("response_id")
        if response_id not in expected:
            continue
        assert response_id not in found, "Multiple archives for one response"
        entry = expected[response_id]
        assert entry["start"] <= record["started_at"] <= entry["end"]
        assert record["id"] not in raw_ids, "Archive reused"
        with gzip.open(record["archive"], "rt") as stream:
            raw = json.load(stream)
        request = raw["request"]
        response = json.loads(raw["response_body"])
        assert digest(request) == entry["request_sha256"], "Saved payload differs"
        assert digest(response) == entry["response_sha256"], "Saved response differs"
        assert response["id"] == response_id
        assert raw["response_status"] == record["http_status"] == 200
        assert request["model"] == response["model"] == "qwen3.8-flash"
        assert request["enable_thinking"] is False and request["temperature"] == 0
        assert request["max_tokens"] == 2048
        assert response["usage"] == entry["usage"] == record["usage"]
        message = response["choices"][0]["message"]
        assert not message.get("reasoning_content")
        assert not (response["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens")
        content_json_valid = True
        try:
            json.loads(message["content"])
        except json.JSONDecodeError:
            content_json_valid = False
        assert content_json_valid == entry.get("content_json_valid", True), "Content JSON validity differs"
        raw_ids.add(record["id"])
        found[response_id] = {
            "group": entry["group"],
            "qa_id": entry["qa_id"],
            "response_id": response_id,
            "archive_id": record["id"],
            "archive": record["archive"],
            "archive_sha256": hashlib.sha256(Path(record["archive"]).read_bytes()).hexdigest(),
            "usage": record["usage"],
            "http_status": record["http_status"],
            "stage": entry["stage"],
            "content_json_valid": content_json_valid,
        }
    assert set(found) == set(expected), sorted(set(expected) - set(found))
    rows = list(found.values())
    return {
        "status": "passed",
        "matched_calls": len(rows),
        "http_status_counts": dict(collections.Counter(str(r["http_status"]) for r in rows)),
        "tokens": sum(r["usage"]["total_tokens"] for r in rows),
        "match": (
            "response ID, complete canonical payload and response SHA, usage, time window; one raw per call"
        ),
        "all_thinking_disabled": True,
        "generated_model_requests": 0,
        "requests": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(json.loads(args.config.read_text())), ensure_ascii=False))
