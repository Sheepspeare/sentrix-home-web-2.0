"""Summarize saved QA scores and classify existing failures without model calls."""

import collections
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from representative_qa import process_environment, save

B = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments")
SERVICE = (
    "arrearage",
    "access denied",
    "overdue-payment",
    "good standing",
    "insufficient_quota",
    "insufficient balance",
    "account is not",
    "account status",
)
INFRA = (
    "connection refused",
    "connection reset",
    "server disconnected",
    "remoteprotocolerror",
    "connecterror",
    "connecttimeout",
    "502 bad gateway",
    "503 service",
    "internal server error",
)


def marks(text):
    low = text.lower()
    return [v for v in SERVICE + INFRA if v in low]


def main(out):
    from photobench.infrastructure.results.store import FileRunStore

    specs = []
    for root in [
        B / "full-four-continue-20261127/prepared",
        B / "planner-large-continue16-20261127/prepared",
    ]:
        for g in json.loads((root / "intent.json").read_text())["groups"]:
            if g["name"] == "baseline-limited":
                continue
            specs.append((root, g))
    summaries = []
    for root, g in specs:
        folder = root / g["name"]
        started = json.loads((folder / "started.json").read_text())
        states = []
        for origin in g["sources"]:
            states.append(FileRunStore(Path(origin["results"])).load_state(origin["run"]))
        states.append(FileRunStore(folder / "results").load_state(started["run_id"]))
        items = {}
        for s in states:
            items.update({v["qa_id"]: v for v in s["items"]})
        scoring = folder / "judge"
        preserved = json.loads((scoring / "preserved-scores.json").read_text())
        scores = {q: v["result"]["normalized_score"] for q, v in preserved.items()}
        for p in sorted(scoring.glob("batch-*/scoring-state.json")):
            s = json.loads(p.read_text())
            for r in s.get("scoring_results", {}).values():
                if (
                    r.get("status") == "completed"
                    and s.get("diagnostic_results", {}).get(r.get("diagnostic_key"), {}).get("status")
                    == "completed"
                ):
                    scores[r["qa_id"]] = r["normalized_score"]
        _, _, env = process_environment(g["product"])
        rows = []
        dbpath = Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db"
        with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True) as db:
            for q, item in items.items():
                turns = item.get("runtime_turns") or []
                ids = [v.get("turn_id") for v in turns if v.get("turn_id")]
                if not ids and item.get("turn_id"):
                    ids = [item["turn_id"]]
                signals = marks(str(item.get("error", "")) + " " + str(item.get("answer", "")))
                if not ids:
                    ids = re.findall(r"turn_\d+", str(item.get("error", "")))
                statuses = []
                reasons = []
                stops = []
                answers = []
                errors = []
                for tid in ids:
                    found = db.execute(
                        "select status,termination_reason,answer,trace_json from agent_turns where id=?",
                        (tid,),
                    ).fetchone()
                    if not found:
                        continue
                    status, reason, answer, raw = found
                    trace = json.loads(raw or "{}")
                    statuses.append(status)
                    reasons.append(reason)
                    answers.append(answer or "")
                    stops.append(trace.get("budget", {}).get("request_limits", {}).get("stop_code"))
                    signals.extend(marks(answer or ""))
                    for c in trace.get("model_calls", []) + trace.get("debug_calls", []):
                        diag = c.get("diagnostics") or {}
                        err = (
                            str(c.get("error") or "")
                            + " "
                            + str(diag.get("error") or "")
                            + " "
                            + str(c.get("error_message") or "")
                            + " "
                            + str(diag.get("error_message") or "")
                        )
                        hit = marks(err)
                        if hit:
                            signals.extend(hit)
                            errors.append(err[:600])
                        h = diag.get("http_status")
                        if isinstance(h, int) and h >= 400:
                            errors.append("HTTP " + str(h) + " " + err[:400])
                    if reason == "model_error":
                        errors.extend(
                            json.dumps(
                                {k: v for k, v in c.items() if "error" in k.lower() or k == "diagnostics"},
                                ensure_ascii=False,
                            )[:1200]
                            for c in trace.get("model_calls", [])
                            if c.get("status") in ("error", "failed")
                        )
                # Score states already freeze the evaluated answer; statuses are separate diagnostics.
                complete = item.get("execution_status") == "completed"
                terminal_failure = not complete and item.get("execution_status") not in (
                    "running",
                    "pending",
                    "scheduled",
                    None,
                )
                row = {
                    "qa_id": q,
                    "execution_status": item.get("execution_status"),
                    "score": scores.get(q),
                    "product_status": statuses,
                    "termination": reasons,
                    "stop": stops,
                    "signals": sorted(set(signals)),
                    "errors": errors,
                    "has_answer": bool(any(answers)),
                    "turn_ids": ids,
                    "execution_error": str(item.get("error") or "")[:500],
                }
                if signals:
                    row["classification"] = "service_or_transport_affected"
                elif terminal_failure and (
                    any(v == "timeout" for v in reasons)
                    or "timeout" in str(item.get("error", "")).lower()
                    or "timed out" in str(item.get("error", "")).lower()
                    or "did not complete within" in str(item.get("error", "")).lower()
                ):
                    row["classification"] = "time_limit_no_answer"
                elif any(v == "model_error" for v in reasons):
                    row["classification"] = "model_error_review"
                elif terminal_failure:
                    row["classification"] = "execution_failure_review"
                else:
                    row["classification"] = "answered"
                rows.append(row)
        answered = [r for r in rows if r["score"] is not None]
        abnormal = [r for r in rows if r["classification"] == "service_or_transport_affected"]
        normal = [r for r in answered if not r["signals"]]
        failures = [r for r in rows if r["classification"] == "time_limit_no_answer"]
        determined = len(answered) + sum(r["score"] is None for r in failures)
        summary = {
            "name": g["name"],
            "processed": len(items),
            "valid_scores": len(answered),
            "score_sum": sum(r["score"] for r in answered),
            "scored_mean": sum(r["score"] for r in answered) / len(answered) if answered else None,
            "normal_scored_n": len(normal),
            "normal_scored_mean": sum(r["score"] for r in normal) / len(normal) if normal else None,
            "time_limit_no_answer": len(failures),
            "determined_with_failure_zero": determined,
            "determined_mean_with_failure_zero": sum(r["score"] for r in answered) / determined
            if determined
            else None,
            "classification": dict(collections.Counter(r["classification"] for r in rows)),
            "execution_status": dict(collections.Counter(r["execution_status"] for r in rows)),
            "termination": dict(collections.Counter(v for r in rows for v in r["termination"])),
            "needs_scoring": sum(r["score"] is None and r["execution_status"] == "completed" for r in rows),
            "affected_scored": sum(r["score"] is not None for r in abnormal),
            "trace_missing": sum(not r["turn_ids"] for r in rows),
            "at": time.time(),
        }
        save(out / (g["name"] + "-items.json"), rows)
        save(out / (g["name"] + "-summary.json"), summary)
        summaries.append(summary)
    save(out / "summary.json", summaries)
    print(json.dumps(summaries, ensure_ascii=False))


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(exist_ok=True)
    main(out)
