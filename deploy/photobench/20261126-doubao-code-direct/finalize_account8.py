"""Validate completed retry evidence and publish explicit answer-version overrides."""

import hashlib
import json
from pathlib import Path

from representative_qa import save
from verify_judge_http import digest, verify


def main():
    from photobench.infrastructure.results.store import FileRunStore

    b = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
    root = b / "planner-account8-retry-20261127"
    selected = json.loads((root / "selection.json").read_text())
    g = json.loads((root / "intent.json").read_text())["group"]
    started = json.loads((root / "started.json").read_text())
    state = FileRunStore(root / "results").load_state(started["run_id"])
    original = FileRunStore(Path(g["sources"][0]["results"])).load_state(g["source_run"])
    definitions = {r["qa_id"]: r for r in original["qa_snapshot"]["rows"]}
    assert len(state["items"]) == 8
    assert all(r == definitions[r["qa_id"]] for r in state["qa_snapshot"]["rows"])
    scoring = json.loads((root / "judge/scoring-state.json").read_text())
    scores = {}
    for v in scoring["scoring_results"].values():
        assert v["status"] == "completed"
        assert scoring["diagnostic_results"][v["diagnostic_key"]]["status"] == "completed"
        scores[v["qa_id"]] = v["normalized_score"]
    assert len(scores) == 7
    expected = []
    for line in (root / "judge/judge-http.jsonl").read_text().splitlines():
        r = json.loads(line)
        assert r["status"] == "completed"
        response = r["response"]
        expected.append(
            {
                "response_id": response["id"],
                "start": r["started_at"],
                "end": r["finished_at"],
                "request_sha256": digest(r["input"]),
                "response_sha256": digest(response),
                "usage": response["usage"],
                "group": "planner-account8",
                "qa_id": "frozen_scoring_batch",
                "stage": "judge",
            }
        )
    verification = verify({"expected": expected, "log": str(root / "judge/http/requests.jsonl")})
    save(root / "judge-http-verification.json", verification)
    old = {r["qa_id"]: r for r in selected["items"]}
    versions = []
    for item in state["items"]:
        q = item["qa_id"]
        assert old[q]["score"] == 0
        completed = item["execution_status"] == "completed"
        if completed:
            assert q in scores
        else:
            assert item["execution_status"] == "timeout"
            assert "3660s" in item["error"]
        versions.append(
            {
                "qa_id": q,
                "run_id": started["run_id"],
                "results": str(root / "results"),
                "score": scores.get(q),
                "failure_zero": not completed,
                "execution_status": item["execution_status"],
                "old_score": 0,
                "definition_sha256": hashlib.sha256(
                    json.dumps(definitions[q], sort_keys=True).encode()
                ).hexdigest(),
            }
        )
    summary = {
        "count": 8,
        "scored": 7,
        "time_limit_zero": 1,
        "score_sum": sum(scores.values()),
        "fixed_mean": sum(scores.values()) / 8,
        "scored_mean": sum(scores.values()) / 7,
        "improved": sum(v > 0 for v in scores.values()),
        "unchanged": sum(v == 0 for v in scores.values()) + 1,
        "judge_http": verification["matched_calls"],
        "judge_tokens": verification["tokens"],
        "original_results_preserved": True,
        "version_overrides": versions,
    }
    save(root / "accepted-version-overrides.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "version_overrides"}))


if __name__ == "__main__":
    main()
