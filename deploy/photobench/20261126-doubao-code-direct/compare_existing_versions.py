"""Read existing 83-question product traces and score states; never call a model."""

import collections
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from representative_qa import process_environment

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
P = B / "representative83-priority-20261127"


def read(path):
    return json.loads(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scores(paths):
    out = {}
    for p in paths:
        state = read(p)
        for v in state.get("scoring_results", {}).values():
            if v.get("status") == "completed":
                out[v["qa_id"]] = v["normalized_score"]
    return out


def trace_records(port, turns):
    _, _, env = process_environment(port)
    path = Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db"
    result = {}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        for qa, turn in turns.items():
            row = db.execute(
                "select question,answer,status,termination_reason,trace_json from agent_turns where id=?",
                (turn,),
            ).fetchone()
            if not row:
                continue
            question, answer, status, termination, raw = row
            t = json.loads(raw)
            calls = t.get("model_calls", [])
            debug = t.get("debug_calls", [])
            agents = [c for c in debug if c.get("call_type") == "agent"]
            planner = [c for c in debug if c.get("call_type") == "planner"]
            toolsets = []
            actions = []
            for c in agents:
                toolsets.append(
                    sorted(v.get("function", {}).get("name", v.get("name", "")) for v in c.get("tools", []))
                )
                response = c.get("response")
                response = response if isinstance(response, dict) else {}
                choice = (response.get("choices") or [{}])[0]
                message = choice.get("message") or response
                actual = message.get("tool_calls") or c.get("tool_calls", [])
                for v in actual:
                    f = v.get("function", v) if isinstance(v, dict) else {"name": v}
                    actions.append({"name": f.get("name"), "arguments": f.get("arguments")})
            requirements = t.get("planner", {}).get("requirements", [])
            types = sorted(r.get("evidence_type", "") for r in requirements)
            normalized = [
                {k: r.get(k) for k in ["evidence_type", "predicate", "value_type", "scope", "conditions"]}
                for r in requirements
            ]
            sent = [c for c in calls if c.get("diagnostics", {}).get("request_sent") is True]
            contexts = sorted(
                {
                    c.get("diagnostics", {}).get("context_window_tokens")
                    for c in calls
                    if c.get("diagnostics", {}).get("context_window_tokens")
                }
            )
            errors = [str(c.get("error", "")) + str(c.get("diagnostics", {}).get("error", "")) for c in debug]
            result[qa] = {
                "question": question,
                "answer": answer,
                "status": status,
                "termination": termination,
                "stop": t.get("budget", {}).get("request_limits", {}).get("stop_code"),
                "planner_status": t.get("planner", {}).get("status"),
                "planner_types": types,
                "planner_requirements": normalized,
                "planner_prompt_sha": digest(planner[0].get("messages", [])[:1]) if planner else None,
                "initial_tools": toolsets[0] if toolsets else [],
                "tool_path": [v["name"] for v in actions],
                "actions": actions,
                "http": len(sent),
                "tokens": sum(
                    (c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0) for c in calls
                ),
                "contexts": contexts,
                "service_error": any(
                    any(s in x for s in ["Arrearage", "Access denied", "overdue-payment", "good standing"])
                    for x in errors
                )
                or any(s in answer for s in ["Access denied", "overdue-payment", "good standing"]),
                "turn_id": turn,
            }
    return result


def compare(a, b, sa, sb):
    ids = sorted(set(a) & set(b))
    counts = collections.Counter()
    strata = collections.defaultdict(list)
    pairs = []
    for q in ids:
        x, y = a[q], b[q]
        checks = {
            "answer_exact": x["answer"] == y["answer"],
            "planner_types_same": x["planner_types"] == y["planner_types"],
            "planner_requirements_same": x["planner_requirements"] == y["planner_requirements"],
            "planner_prompt_same": x["planner_prompt_sha"] == y["planner_prompt_sha"],
            "initial_tools_same": x["initial_tools"] == y["initial_tools"],
            "tool_path_same": x["tool_path"] == y["tool_path"],
            "actions_exact": x["actions"] == y["actions"],
            "termination_same": x["termination"] == y["termination"],
            "stop_same": x["stop"] == y["stop"],
        }
        for k, v in checks.items():
            counts[k] += int(v)
        score = None
        if q in sa and q in sb:
            score = "same" if sa[q] == sb[q] else "improved" if sb[q] > sa[q] else "regressed"
            counts["score_" + score] += 1
        segment = str(x["contexts"]) + " -> " + str(y["contexts"])
        strata[segment].append(q)
        pairs.append(
            {
                "qa_id": q,
                "question": x["question"],
                **checks,
                "score_change": score,
                "score_a": sa.get(q),
                "score_b": sb.get(q),
                "a_contexts": x["contexts"],
                "b_contexts": y["contexts"],
                "a_termination": x["termination"],
                "b_termination": y["termination"],
                "a_http": x["http"],
                "b_http": y["http"],
                "a_tokens": x["tokens"],
                "b_tokens": y["tokens"],
                "service_error": x["service_error"] or y["service_error"],
                "a_tools": x["tool_path"],
                "b_tools": y["tool_path"],
                "a_planner_types": x["planner_types"],
                "b_planner_types": y["planner_types"],
                "a_answer": x["answer"],
                "b_answer": y["answer"],
            }
        )
    clean = [p for p in pairs if p["a_contexts"] == p["b_contexts"] == [131072] and not p["service_error"]]

    def subset(rows):
        scored = [r for r in rows if r["score_change"]]
        return {
            "n": len(rows),
            "scored_pairs": len(scored),
            "same": sum(r["score_change"] == "same" for r in scored),
            "improved": sum(r["score_change"] == "improved" for r in scored),
            "regressed": sum(r["score_change"] == "regressed" for r in scored),
            "a_sum": sum(r["score_a"] for r in scored),
            "b_sum": sum(r["score_b"] for r in scored),
            "answer_exact": sum(r["answer_exact"] for r in rows),
            "tool_path_same": sum(r["tool_path_same"] for r in rows),
            "initial_tools_same": sum(r["initial_tools_same"] for r in rows),
            "a_http": sum(r["a_http"] for r in rows),
            "b_http": sum(r["b_http"] for r in rows),
            "a_tokens": sum(r["a_tokens"] for r in rows),
            "b_tokens": sum(r["b_tokens"] for r in rows),
        }

    return {
        "trace_pairs": len(ids),
        "counts": dict(counts),
        "all": subset(pairs),
        "context_strata": {k: len(v) for k, v in strata.items()},
        "strict_128k_no_account_error": subset(clean),
        "pairs": pairs,
    }


def main(root):
    root.mkdir(exist_ok=True)
    views = read(P / "view83-four-groups-final.json")
    plan = read(P / "intent.json")
    specs = {}
    for budget, port in [("limited", 8305), ("large", 8306)]:
        turns = read(P / f"old-code83-{budget}-raw-matches.json")["turns"]
        g = next(v for v in plan["groups"] if v["name"] == budget)
        paths = [Path(v["path"]) for v in g["previous_states"]] + [P / budget / "scoring-state.json"]
        paths += sorted(
            (B / "representative83-missing-qa-20261127-v7" / budget).glob("judge*/batch-*/scoring-state.json")
        )
        specs["old_" + budget] = (port, turns, paths)
        specs["planner_" + budget] = (
            8296 if budget == "limited" else 8297,
            views["planner_" + budget]["turns"],
            [root / ("baseline-" + budget + "-scores-only.json")],
        )
    specs["improved_limited"] = (
        8304,
        views["fusion_limited"]["turns"],
        [B / "full-four-continue-20261127/improved-limited-existing-scores.json"],
    )
    g = next(v for v in plan["groups"] if v["name"] == "fusion")
    paths = (
        [Path(v["path"]) for v in g["previous_states"]]
        + [P / "fusion/scoring-state.json"]
        + sorted((B / "fusion83-account-retry-20261127/fusion/judge").glob("batch-*/scoring-state.json"))
    )
    specs["improved_large"] = (8303, views["fusion_large"]["turns"], paths)
    records, grades = {}, {}
    for name, (port, turns, paths) in specs.items():
        records[name] = trace_records(port, turns)
        grades[name] = scores(paths)
        (root / (name + "-records.json")).write_text(json.dumps(records[name], ensure_ascii=False))
    output = {}
    for budget in ["limited", "large"]:
        for version in ["planner", "improved"]:
            a, b = "old_" + budget, version + "_" + budget
            output[a + "_vs_" + b] = compare(records[a], records[b], grades[a], grades[b])
    (root / "paired-details.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    summary = {k: {a: b for a, b in v.items() if a != "pairs"} for k, v in output.items()}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
