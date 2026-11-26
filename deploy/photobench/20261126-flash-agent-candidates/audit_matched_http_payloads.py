"""Audit already matched HTTP archives, including failed attempts and image payloads."""

import argparse
import base64
import collections
import gzip
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def audit(records):
    rows = []
    seen_calls, seen_archives = set(), set()
    for record in records:
        assert record["call_id"] not in seen_calls, "Call reused"
        assert record["id"] not in seen_archives, "Archive reused"
        seen_calls.add(record["call_id"])
        seen_archives.add(record["id"])
        path = Path(record["archive"])
        archive_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert archive_sha == record["sha256"], "Matched archive changed"
        with gzip.open(path, "rt") as stream:
            raw = json.load(stream)
        request = raw["request"]
        assert request["model"] == "qwen3.8-flash"
        assert request["enable_thinking"] is False
        assert request["temperature"] == 0 and 0 < request["max_tokens"] <= 4096
        assert raw["response_status"] == record["http_status"]
        images = []
        for message in request.get("messages", []):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if part.get("type") != "image_url":
                    continue
                url = part["image_url"]["url"]
                assert url.startswith("data:image/") and ";base64," in url
                payload = base64.b64decode(url.split(",", 1)[1], validate=True)
                assert payload, "Empty image payload"
                images.append({"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        assert bool(images) == record["has_image"], "Image metadata differs from payload"
        body = raw["response_body"]
        row = {
            "call_id": record["call_id"],
            "qa_id": record["qa_id"],
            "archive_id": record["id"],
            "archive_sha256": archive_sha,
            "request_sha256": digest(request),
            "response_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "http_status": record["http_status"],
            "images": images,
        }
        if record["http_status"] == 200:
            response = json.loads(body)
            assert response["id"] == record["response_id"]
            assert response["model"] == "qwen3.8-flash"
            assert response["usage"] == record["usage"]
            assert not any(c.get("message", {}).get("reasoning_content") for c in response["choices"])
            assert not (response["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens")
            row["usage"] = response["usage"]
        else:
            assert not record.get("usage"), "Failed response usage needs explicit review"
            row["usage"] = None
        rows.append(row)
    return {
        "status": "passed",
        "matched_calls": len(rows),
        "http_status_counts": dict(collections.Counter(str(r["http_status"]) for r in rows)),
        "known_total_tokens": sum(r["usage"]["total_tokens"] for r in rows if r["usage"] is not None),
        "usage_missing_calls": [r["call_id"] for r in rows if r["usage"] is None],
        "visual_HTTP": sum(bool(r["images"]) for r in rows),
        "image_payloads": sum(len(r["images"]) for r in rows),
        "all_thinking_disabled": True,
        "successful_responses_have_no_reasoning": True,
        "generated_model_requests": 0,
        "requests": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("matched", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(json.loads(args.matched.read_text())["requests"]), ensure_ascii=False))
