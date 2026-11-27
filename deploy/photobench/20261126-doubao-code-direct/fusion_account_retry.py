"""Freeze and launch only the 24 account-failure representative retries."""

import argparse
import hashlib
import json
import pathlib
import shlex
import sqlite3
import time
import urllib.request

from representative_qa import get, process_environment, save


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(root, score_root):
    assert not root.exists()
    plan = json.loads((score_root / "intent.json").read_text())
    group = next(g for g in plan["groups"] if g["name"] == "fusion")
    original = score_root / "fusion/input.json"
    source = json.loads(original.read_text())
    selected = [
        v
        for v in source["items"]
        if any(
            t in str(v.get("answer", "")).lower()
            for t in ["access denied", "overdue-payment", "good standing"]
        )
    ]
    ids = [v["qa_id"] for v in selected]
    assert len(ids) == len(set(ids)) == 24
    scores = {v["qa_id"]: v for v in json.loads((score_root / "fusion/scores.json").read_text())}
    assert all(scores[q]["normalized_score"] == 0 for q in ids)
    live = get(8883, f"runs/{group['run_id']}?include_history=false")
    assert live["phases"]["qa_eval"]["progress"]["dispatch_paused"]
    product_pid, _, product_env = process_environment(8303)
    db_path = pathlib.Path(product_env["SENTRIX_DATA_DIR"]) / "sentrix.db"
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        assert (
            db.execute("select count(*) from agent_turns where status in ('pending','running')").fetchone()[0]
            == 0
        )
    cfg = get(8303, "diagnostics")["runtime_configuration"]
    assert cfg["reasoning_max_model_calls"] == 100 and cfg["reasoning_max_tool_calls"] == 600
    assert cfg["vlm_context_tokens"] == 131072 and cfg["agent_turn_workers"] == 16
    root.mkdir()
    # A single authenticated cloud request establishes current account availability.
    key = next(
        shlex.split(s.split("=", 1)[1])[0]
        for s in pathlib.Path(plan["secret_env"]).read_text().splitlines()
        if s.startswith("QWEN_API_KEY=")
    )
    protocol = json.loads((score_root / "protocol.json").read_text())
    payload = {
        "model": "qwen3.8-flash",
        "messages": [{"role": "user", "content": "Reply OK."}],
        "max_tokens": 1,
        "temperature": 0,
        "enable_thinking": False,
    }
    request = urllib.request.Request(
        protocol["url"] + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        probe = json.load(response)
    save(root / "cloud-preflight.json", {"at": time.time(), "input": payload, "response": probe})
    snapshot = json.loads(pathlib.Path(group["previous_states"][0]["path"]).read_text())["qa_snapshot"]
    assert snapshot == source["qa_snapshot"]
    definitions = {v["qa_id"]: v for v in snapshot["rows"]}
    evidence = [
        {
            "qa_id": v["qa_id"],
            "original_answer_sha256": hashlib.sha256(v["answer"].encode()).hexdigest(),
            "original_score": scores[v["qa_id"]]["normalized_score"],
            "definition_sha256": hashlib.sha256(
                json.dumps(definitions[v["qa_id"]], sort_keys=True).encode()
            ).hexdigest(),
            "original_revision": v.get("trace_storage", {}).get("revision"),
            "selection": "answer contains cloud account error",
        }
        for v in selected
    ]
    save(
        root / "selection.json",
        {
            "count": 24,
            "source_run": group["run_id"],
            "source_input_sha256": digest(original),
            "source_scores_sha256": digest(score_root / "fusion/scores.json"),
            "items": evidence,
        },
    )
    pid, words, _ = process_environment(8883)
    g = {
        "name": "fusion",
        "port": 8883,
        "product": 8303,
        "source_run": group["run_id"],
        "eval_pid": pid,
        "evaluation_source": words[words.index("--app-dir") + 1],
        "missing_ids": [v["qa_id"] for v in snapshot["rows"] if v["qa_id"] in ids],
        "present_ids": [q for q in plan["representative_ids"] if q not in ids],
        "db_path": str(db_path),
        "qa_count": 24,
    }
    save(
        root / "intent.json",
        {
            "score_root": str(score_root),
            "groups": [g],
            "created_at": time.time(),
            "scope": "only24accountfailure",
            "full_dispatch_remains_paused": True,
            "original_product_pid": product_pid,
        },
    )
    print(
        json.dumps(
            {
                "selected": 24,
                "cloud_preflight": "success",
                "active_product_turns": 0,
                "product_pid": product_pid,
            }
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", type=pathlib.Path)
    p.add_argument("score_root", type=pathlib.Path)
    a = p.parse_args()
    prepare(a.root, a.score_root)
