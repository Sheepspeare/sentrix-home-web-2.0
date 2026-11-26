"""Match saved turn observations to existing gateway archives without generating requests."""

import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path


def matches(expected, record, request, questions):
    if not expected["start"] <= record["started_at"] <= expected["end"]:
        return False
    if "cached_tokens" in expected:
        cached = (record.get("usage") or {}).get("prompt_tokens_details") or {}
        if cached.get("cached_tokens") != expected["cached_tokens"]:
            return False
    messages = request.get("messages", [])
    texts = [m["content"].strip() for m in messages if isinstance(m.get("content"), str)]
    if not expected["internal"]:
        return questions[expected["qa_id"]].strip() in texts
    parts = [
        p.get("text", "")
        for m in messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if isinstance(p, dict)
    ]
    return not expected.get("asked") or any(expected["asked"] in t for t in texts + parts)


def verify(config):
    expected_by_signature = collections.defaultdict(list)
    found = {}
    for e in config["expected"]:
        assert e["id"] not in found
        found[e["id"]] = None
        expected_by_signature[tuple(e["signature"])].append(e)
    finalization = []
    archive_ids = set()
    for line in Path(config["log"]).read_text().splitlines():
        record = json.loads(line)
        if not config["start"] <= record["started_at"] <= config["end"]:
            continue
        usage = record.get("usage") or {}
        signature = (
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            (record.get("finish_reasons") or [None])[0],
            record.get("has_image"),
        )
        candidates = expected_by_signature.get(signature, [])
        if not candidates:
            continue
        with gzip.open(record["archive"], "rt") as f:
            raw = json.load(f)
        request = raw["request"]
        candidates = [e for e in candidates if matches(e, record, request, config["questions"])]
        if not candidates:
            continue
        assert len({e["qa_id"] for e in candidates}) == 1, (signature, "ambiguous turns")
        available = [e for e in candidates if found[e["id"]] is None]
        assert available, (signature, "extra archive matches")
        e = available[0]
        assert record["id"] not in archive_ids
        archive_ids.add(record["id"])
        assert request["model"] == "qwen3.8-flash" and request["enable_thinking"] is False
        assert request.get("temperature") == 0 and 0 < request["max_tokens"] <= 4096
        found[e["id"]] = {
            **record,
            "qa_id": e["qa_id"],
            "call_id": e["id"],
            "sha256": hashlib.sha256(Path(record["archive"]).read_bytes()).hexdigest(),
        }
        messages = request.get("messages", [])
        if any(
            m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and "取证请求额度已用完" in m["content"]
            for m in messages
        ):
            names = [t["function"]["name"] for t in request.get("tools", [])]
            finalization.append(
                {
                    "qa_id": e["qa_id"],
                    "call_id": e["id"],
                    "archive": record["archive"],
                    "tool_names": names,
                    "prior_tool_messages": sum(m.get("role") == "tool" for m in messages),
                    "message_count": len(messages),
                    "messages_sha256": hashlib.sha256(
                        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                    "reply_tool_names": [
                        c["function"]["name"]
                        for c in json.loads(raw["response_body"])
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("tool_calls", [])
                    ],
                }
            )
    assert all(v is not None for v in found.values()), [k for k, v in found.items() if v is None]
    requests = list(found.values())
    return {
        "matched_calls": len(requests),
        "http_status_counts": dict(collections.Counter(str(r["http_status"]) for r in requests)),
        "requests": requests,
        "budget_finalization": finalization,
        "match": (
            "usage/finish/image, cached tokens when recorded, full question or visual prompt, "
            "per-turn time window; one use per call/raw"
        ),
        "generated_model_requests": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(json.loads(args.config.read_text())), ensure_ascii=False))
