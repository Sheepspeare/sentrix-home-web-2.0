"""Collect four existing terminal turns, then resume only nineteen unstarted QA."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sqlite3
import sys
import urllib.request
import uuid
from datetime import datetime

from inspect_reconnect import APP, BASE, DB, EVALUATOR, HERE, ROOT, RUN, fingerprint, pid, sha
from probe_turns import snapshot

sys.path.insert(0, str(APP))
from photobench.infrastructure.results.split_store import SplitResultWriter, SplitRunReader  # noqa: E402

SEED = "20261122-doubao-reboot-turns-seed-f72710"
TERMINAL = {"complete", "partial", "failed", "cancelled"}


def read(name):
    return json.loads((HERE / name).read_text())


def save(name, data):
    with (HERE / name).open("x") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def now():
    return datetime.now().astimezone().isoformat()


def api(port, path, payload=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def source():
    plan = read("inventory.json")
    assert sha(ROOT / RUN / "split-commit.json") == plan["manifest_sha256"]
    state = SplitRunReader(ROOT, ROOT / RUN).state()
    assert state["status"] == "interrupted" and len(state["items"]) == 990
    items = {i["qa_id"]: i for i in state["items"]}
    assert all(fingerprint(items[q]) == value for q, value in plan["item_fingerprints"].items())
    assert fingerprint(state["scoring_results"]) == plan["scoring_results_fingerprint"]
    for old, fact in plan["prior_runs"].items():
        assert sha(ROOT / old / "split-commit.json") == fact["manifest_sha256"]
    return state


def idle():
    result = snapshot()
    for name in (
        "service_pids_match",
        "evaluation_app_matches",
        "writer_source_matches",
        "source_manifest_unchanged",
        "original_manifest_unchanged",
        "active_mapping_matches",
        "model_matches",
        "product_limits_match",
        "product_database_match",
        "all_terminal",
    ):
        assert result[name], name
    assert not result["all_live_turns"] and not result["active_evaluation"]
    assert result["imports"]["postprocess"]["state"] == "idle"
    assert result["imports"]["library"]["processing"] == 0
    assert result["reasoning_context_tokens"] == 16384
    assert min(result["disk_free"].values()) > 1536 * 1024**2
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for table in ("import_jobs", "video_jobs"):
            counts = dict(db.execute(f"select status,count(*) from {table} group by status"))
            assert not any(
                counts.get(s)
                for s in ("accepted", "queued", "running", "processing", "indexing", "extracting")
            ), counts
        assert (
            db.execute(
                "select count(*) from scope_postprocess where processing_generation is not null"
            ).fetchone()[0]
            == 0
        )
        old = result["running_pipeline_receipts"]
        assert {r["operation_id"] for r in old} <= {"8dc3d339db5e429daf3bb2865e614f39"}
        if old:
            replacement = db.execute(
                "select status,started_at from pipeline_receipts where operation_id=?",
                ("141834a96b9c4c0989ececbf326c4981",),
            ).fetchone()
            assert replacement["status"] == "completed" and replacement["started_at"] > old[0]["started_at"]
        remaining = read("inventory.json")["never_started_qa_ids"]
        assert len(remaining) == 19
        for qa in remaining:
            assert (
                db.execute(
                    "select count(*) from agent_turns t join agent_conversations c on c.id=t.conversation_id "
                    "where t.scope_id=? and c.title=?",
                    (read("inventory.json")["scope_id"], qa),
                ).fetchone()[0]
                == 0
            ), qa
    return result


def terminal_facts():
    assert not (HERE / "terminal-facts.json").exists()
    activity = idle()
    state = source()
    rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    facts = []
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for original in read("inventory.json")["uncollected_product_turns"]:
            row = db.execute(
                "select id,conversation_id,status,termination_reason,created_at,updated_at,"
                "attempt_no,answer,question "
                "from agent_turns where id=?",
                (original["id"],),
            ).fetchone()
            fact = dict(row)
            answer = fact.pop("answer") or ""
            assert fact.pop("question") == rows[original["qa_id"]]["question"]
            assert fact["status"] in TERMINAL
            assert fact["conversation_id"] == original["conversation_id"]
            fact.update(
                qa_id=original["qa_id"],
                answer_chars=len(answer),
                answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
            )
            body = api(8294, "assistant/turns/" + fact["id"])
            assert body["status"] == fact["status"]
            assert (
                hashlib.sha256((body.get("result", {}).get("answer") or "").encode()).hexdigest()
                == fact["answer_sha256"]
            )
            facts.append(fact)
    save(
        "terminal-facts.json",
        {
            "observed_at": now(),
            "source_run_id": RUN,
            "facts": facts,
            "activity": activity,
            "product_http_methods": ["GET"],
            "agent_rerun": False,
        },
    )
    print(json.dumps({"terminal_turns": facts, "never_started": 19}, ensure_ascii=False), flush=True)


class ExistingTurn:
    def __init__(self, fact, question, scope):
        self.fact, self.question, self.scope = fact, question, scope

    def start_conversation(self, *, title, scope_id):
        assert title == self.fact["qa_id"] and scope_id == self.scope
        return {"id": self.fact["conversation_id"]}

    def start_turn(self, *, conversation_id, message, reasoning, include_debug, **kwargs):
        assert conversation_id == self.fact["conversation_id"] and message == self.question
        assert reasoning and include_debug and kwargs.get("graph_enabled", True)
        return {"turn_id": self.fact["id"]}

    def get_turn(self, *, turn_id, **kwargs):
        assert turn_id == self.fact["id"]
        body = api(8294, "assistant/turns/" + turn_id)
        assert body["status"] == self.fact["status"]
        assert (
            hashlib.sha256((body.get("result", {}).get("answer") or "").encode()).hexdigest()
            == self.fact["answer_sha256"]
        )
        return body


def restore(wait_id):
    from photobench.application.run_execution.service import RunExecution, RunRequest
    from photobench.domain.runs.execution import index_assets_by_name

    uuid.UUID(wait_id)
    assert not (HERE / "restore-four-intent.json").exists() and not (ROOT / SEED).exists()
    idle()
    state = source()
    plan = read("inventory.json")
    facts = read("terminal-facts.json")["facts"]
    assert len(facts) == 4 and {f["id"] for f in facts} == {
        f["id"] for f in plan["uncollected_product_turns"]
    }
    rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    existing = {i["qa_id"] for i in state["items"]}
    assert not existing.intersection(f["qa_id"] for f in facts)
    save(
        "restore-four-intent.json",
        {
            "created_at": now(),
            "wait_id": wait_id,
            "source_run": RUN,
            "seed_run": SEED,
            "facts": facts,
            "product_http_methods": ["GET"],
            "agent_rerun": False,
            "manifest_sha256": plan["manifest_sha256"],
        },
    )
    assets = []
    while True:
        page = api(
            8294, f"assets?scope_id={state['scope_id']}&limit=500&offset={len(assets)}&sort=created&order=asc"
        )
        assets.extend(page["items"])
        if len(assets) >= page["total"]:
            break
        assert page["items"]
    assets_by_name = index_assets_by_name(assets)
    restored = []
    for fact in facts:
        qa = fact["qa_id"]
        execution = RunExecution(
            client=ExistingTurn(fact, rows[qa]["question"], state["scope_id"]),
            albums=None,
            writer=None,
            lifecycle=None,
            inference=None,
            telemetry=None,
        )
        request = RunRequest(
            album_id=state["album_id"],
            qa_set=state["qa_set"],
            model_profile=state["model_profile"],
            sentrix_url="http://127.0.0.1:8294",
            reasoning=True,
            graph_enabled=True,
            turn_timeout=3660,
        )
        item = execution._evaluate_one(rows[qa], state["scope_id"], assets_by_name, request)
        assert item["runtime_turns"][0]["agent_status"] == fact["status"]
        assert item["execution_status"] == (
            "completed" if fact["status"] in {"complete", "partial"} else "failed"
        )
        assert hashlib.sha256(item["answer"].encode()).hexdigest() == fact["answer_sha256"]
        item["wall_clock_ms"] = round(
            (
                datetime.fromisoformat(fact["updated_at"]) - datetime.fromisoformat(fact["created_at"])
            ).total_seconds()
            * 1000,
            1,
        )
        item["product_turn_recovery"] = {**fact, "source_run_id": RUN, "agent_rerun": False}
        state["items"].append(item)
        restored.append({**fact, "execution_status": item["execution_status"]})
    order = {qa: i for i, qa in enumerate(rows)}
    state["items"].sort(key=lambda item: order[item["qa_id"]])
    state.update(run_id=SEED, status="interrupted", current_phase=None, resumed_from=RUN)
    state["reboot_turn_recovery"] = {"source_run_id": RUN, "turns": restored}
    receipt = SplitResultWriter(ROOT).write(ROOT / SEED, state)
    verified = SplitRunReader(ROOT, ROOT / SEED).state()
    saved = {i["qa_id"]: i for i in verified["items"]}
    assert len(saved) == 994
    assert all(fingerprint(saved[q]) == value for q, value in plan["item_fingerprints"].items())
    assert fingerprint(verified["scoring_results"]) == plan["scoring_results_fingerprint"]
    assert set(rows) - set(saved) == set(plan["never_started_qa_ids"])
    source()
    save(
        "restore-four-response.json",
        {
            "created_at": now(),
            "seed_run_id": SEED,
            "items": 994,
            "remaining": 19,
            "restored": restored,
            "write": receipt,
            "all_990_items_and_823_valid_scores_unchanged": True,
            "source_manifest_unchanged": True,
        },
    )
    print(json.dumps({"seed_run_id": SEED, "items": 994, "remaining": 19}), flush=True)


def resume(wait_id):
    uuid.UUID(wait_id)
    assert not (HERE / "resume19-intent.json").exists()
    idle()
    source()
    assert read("restore-four-response.json")["remaining"] == 19
    assert read("restore-four-intent.json")["wait_id"] == wait_id
    recovery = api(8874, f"runs/{SEED}/recovery-plan")
    assert recovery["can_resume"] and recovery["inherited_qa_count"] == 994
    assert recovery["remaining_qa_count"] == 19 and not recovery["product_version_changed"]
    assert recovery["frozen_scoring_profiles"] == ["atm_qs_v1"] and not recovery["judge_use_gt_images"]
    mapping = json.loads((BASE / "active-runs.json").read_text())
    payload = {"repair_missing_judges": False}
    save(
        "resume19-intent.json",
        {
            "created_at": now(),
            "wait_id": wait_id,
            "source_run": SEED,
            "interrupted_run": RUN,
            "recovery_plan": recovery,
            "payload": payload,
            "remaining_qa_ids": read("inventory.json")["never_started_qa_ids"],
            "evaluation_pid": pid(EVALUATOR),
        },
    )
    result = api(8874, f"runs/{SEED}/resume", payload)
    save("resume19-response.json", result)
    assert result["resumed_from"] == SEED and result["resumed_items"] == 994
    assert result["scope_id"] == read("inventory.json")["scope_id"] and not result["retried_qa_ids"]
    assert json.loads((BASE / "active-runs.json").read_text()) == mapping
    mapping["doubao"] = result
    temporary = HERE / "active-runs.resume19.json"
    temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
    temporary.replace(BASE / "active-runs.json")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("inspect", "restore", "resume"))
    parser.add_argument("--wait-id")
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    if args.mode == "inspect":
        terminal_facts()
    else:
        assert args.wait_id
        {"restore": restore, "resume": resume}[args.mode](args.wait_id)
