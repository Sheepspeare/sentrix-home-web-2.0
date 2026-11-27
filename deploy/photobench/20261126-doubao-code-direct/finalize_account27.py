"""Validate completed retry evidence and publish explicit answer-version overrides."""

import gzip
import hashlib
import json
from pathlib import Path

from representative_qa import save
from verify_judge_http import digest


def main():
    from photobench.infrastructure.results.store import FileRunStore

    b = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
    root = b / "baseline-account27-recovery2-20261127"
    original_root = b / "baseline-account27-retry-20261127"
    selected = json.loads((original_root / "selection.json").read_text())
    plan = json.loads((original_root / "intent.json").read_text())["group"]
    original = FileRunStore(Path(plan["sources"][0]["results"])).load_state(plan["source_run"])
    definitions = {r["qa_id"]: r for r in original["qa_snapshot"]["rows"]}
    old = {r["qa_id"]: r for r in selected["items"]}
    versions, all_scores, verifications = [], {}, []
    started = json.loads((root / "missing9/started.json").read_text())
    sources = [
        (root / "preserved-results", "preserved-account27", root / "preserved-scoring/judge", 18),
        (root / "missing9/results", started["run_id"], root / "missing9/judge", 9),
    ]
    for results, run_id, score_root, count in sources:
        state = FileRunStore(results).load_state(run_id)
        assert len(state["items"]) == count
        assert all(r == definitions[r["qa_id"]] for r in state["qa_snapshot"]["rows"])
        scoring = json.loads((score_root / "scoring-state.json").read_text())
        scores = {}
        for value in scoring["scoring_results"].values():
            assert value["status"] == "completed"
            assert scoring["diagnostic_results"][value["diagnostic_key"]]["status"] == "completed"
            scores[value["qa_id"]] = value["normalized_score"]
        expected = []
        for line in (score_root / "judge-http.jsonl").read_text().splitlines():
            r = json.loads(line)
            assert r["status"] == "completed"
            response = r["response"]
            expected.append(
                {
                    "response_id": response["id"],
                    "start": r["started_at"],
                    "end": r["finished_at"],
                    "request_sha256": digest(dict(r["input"], stream=False)),
                    "response_sha256": digest(response),
                    "usage": response["usage"],
                    "group": "baseline-account27",
                    "qa_id": "frozen_scoring_batch",
                    "stage": "judge",
                }
            )
        records = [json.loads(line) for line in (score_root / "http/requests.jsonl").read_text().splitlines()]
        matched = []
        for entry in expected:
            matches = [r for r in records if r["response_id"] == entry["response_id"]]
            assert len(matches) == 1
            record = matches[0]
            with gzip.open(record["archive"], "rt") as stream:
                raw = json.load(stream)
            assert digest(raw["request"]) == entry["request_sha256"]
            assert (
                hashlib.sha256(
                    json.dumps(raw["request"], ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest()
                == record["request_sha256"]
            )
            assert digest(raw["response"]) == entry["response_sha256"]
            assert raw["metadata"]["response_sha256"] == record["response_sha256"]
            assert record["http_status"] == 200
            assert entry["start"] <= record["started_at"] <= entry["end"]
            assert raw["response"]["usage"] == entry["usage"] == record["usage"]
            request = raw["request"]
            assert request["enable_thinking"] is False and request["temperature"] == 0
            assert request["max_tokens"] == 2048
            assert not raw["response"]["choices"][0]["message"].get("reasoning_content")
            matched.append(record["id"])
        assert len(matched) == len(set(matched)) == len(expected)
        verification = {
            "status": "passed",
            "matched_calls": len(matched),
            "tokens": sum(r["usage"]["total_tokens"] for r in records),
            "generated_model_requests": 0,
        }
        save(score_root / "judge-http-verification.json", verification)
        verifications.append(verification)
        for item in state["items"]:
            q = item["qa_id"]
            assert q in old and q not in {v["qa_id"] for v in versions}
            completed = item["execution_status"] == "completed"
            if completed:
                assert q in scores
                assert not any(
                    s in str(item.get("answer", "")).lower()
                    for s in ("access denied", "overdue-payment", "good standing", "arrearage")
                )
            else:
                assert item["execution_status"] == "timeout"
                assert "3660s" in item["error"]
            versions.append(
                {
                    "qa_id": q,
                    "run_id": run_id,
                    "results": str(results),
                    "score": scores.get(q),
                    "failure_zero": not completed,
                    "execution_status": item["execution_status"],
                    "old_score": old[q]["score"],
                    "definition_sha256": digest(definitions[q]),
                    "answer_item_sha256": digest(item),
                    "scoring_state_sha256": hashlib.sha256(
                        (score_root / "scoring-state.json").read_bytes()
                    ).hexdigest(),
                }
            )
        all_scores.update(scores)
    assert len(versions) == 27 and {v["qa_id"] for v in versions} == set(old)
    summary = {
        "count": 27,
        "scored": len(all_scores),
        "time_limit_zero": sum(v["failure_zero"] for v in versions),
        "score_sum": sum(all_scores.values()),
        "fixed_mean": sum(all_scores.values()) / 27,
        "scored_mean": sum(all_scores.values()) / len(all_scores),
        "improved": sum((v["score"] or 0) > (v["old_score"] or 0) for v in versions),
        "unchanged": sum((v["score"] or 0) == (v["old_score"] or 0) for v in versions),
        "regressed": sum((v["score"] or 0) < (v["old_score"] or 0) for v in versions),
        "judge_http": sum(v["matched_calls"] for v in verifications),
        "judge_tokens": sum(v["tokens"] for v in verifications),
        "original_results_preserved": True,
        "version_overrides": versions,
    }
    save(root / "accepted-version-overrides.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "version_overrides"}))


if __name__ == "__main__":
    main()
