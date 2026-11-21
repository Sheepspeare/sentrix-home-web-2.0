"""Apply the inspected writer-only patch and continue only never-started Doubao QA.

Every state-changing operation has a separate, exclusive intent and receipt.
The original failed run is immutable; existing terminal product turns are GET-only.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
import pstats
import resource
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
HERE = Path(__file__).resolve().parent
OLD = BASE / "wait-chat-950b2be/evaluation"
CANDIDATE = HERE / "candidate"
APP = CANDIDATE / "apps/photobench/src"
ROOT = BASE / "cloud-results"
DB = BASE / "doubao-reuse-49f347c/product-data/sentrix.db"
RUN = "20261120-020624-2026.11.17-atm-sources-addresses-current-d52ecf"
SEED = "20261121-doubao-terminal-turns-seed-d52ecf"
BATCH = "score-dd0413c8aa869b1c84626de7"
UNIT = "sentrix-atm100-doubao-photobench.service"
PRODUCT = "sentrix-atm100-doubao-api.service"
PROTECTED = (
    "sentrix-atm100-api.service",
    "sentrix-atm100-photobench.service",
    "sentrix-atm100-model.service",
    "sentrix-atm100-wemm.service",
    PRODUCT,
    "sentrix-atm100-qwen37-gateway.service",
    "sentrix-atm100-doubao-gateway.service",
    "sentrix2-api.service",
    "sentrix2-photobench.service",
)


def now():
    return datetime.now().astimezone().isoformat()


def read(name):
    return json.loads((HERE / name).read_text())


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def api(port, path, payload=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def pid(unit):
    return int(
        subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True)
    )


def command(process):
    return Path(f"/proc/{process}/cmdline").read_bytes().decode().strip("\0").split("\0")


def environment(process):
    return dict(p.split("=", 1) for p in Path(f"/proc/{process}/environ").read_text().split("\0") if "=" in p)


def identity(value):
    from photobench.infrastructure.results.split_json import STORED_TYPES

    if isinstance(value, STORED_TYPES):
        return {"immutable_object_ref": value.ref}
    if isinstance(value, dict):
        return {k: identity(v) for k, v in value.items()}
    if isinstance(value, list):
        return [identity(v) for v in value]
    return value


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(identity(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_source():
    from photobench.infrastructure.results.split_store import SplitRunReader

    plan = read("inventory.json")
    assert sha(ROOT / RUN / "split-commit.json") == plan["source_manifest_sha256"]
    assert read_mapping()["doubao"]["run_id"] == RUN
    state = SplitRunReader(ROOT, ROOT / RUN).state()
    assert fingerprint(state["items"]) == plan["items_fingerprint"]
    assert fingerprint(state["scoring_results"]) == plan["scoring_results_fingerprint"]
    assert state["status"] == "failed" and len(state["items"]) == 887
    return state


def read_mapping():
    return json.loads((BASE / "active-runs.json").read_text())


def idle():
    assert not api(8874, "active-work")["work"]
    imports = api(8294, "imports/progress")
    assert imports["library"]["processing"] == 0 and imports["postprocess"]["state"] == "idle"
    with sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        assert not db.execute("select id from agent_turns where status in ('pending','running')").fetchall()
        jobs = {}
        for table in ("import_jobs", "video_jobs"):
            jobs[table] = dict(db.execute(f"select status,count(*) from {table} group by status"))
            assert not any(
                jobs[table].get(s)
                for s in ("accepted", "queued", "running", "processing", "indexing", "extracting")
            ), jobs
        assert (
            db.execute(
                "select count(*) from scope_postprocess where processing_generation is not null"
            ).fetchone()[0]
            == 0
        )
        old = [
            dict(r)
            for r in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
        assert {r["operation_id"] for r in old} <= {"8dc3d339db5e429daf3bb2865e614f39"}, old
        if old:
            replacement = db.execute(
                "select status,started_at from pipeline_receipts where operation_id=?",
                ("141834a96b9c4c0989ececbf326c4981",),
            ).fetchone()
            assert replacement["status"] == "completed" and replacement["started_at"] > old[0]["started_at"]
    return {"imports": imports, "jobs": jobs, "historical_superseded_pipeline_receipts": old}


def limits():
    env = environment(pid(PRODUCT))
    assert Path(env["SENTRIX_DATA_DIR"]) / "sentrix.db" == DB
    assert env["SENTRIX_VLM_BASE_URL"] == "http://127.0.0.1:18107/v1"
    keys = ("MODEL", "TOOL", "SEARCH", "OTHER_TOOL")
    assert [int(env[f"SENTRIX_REASONING_MAX_{k}_CALLS"]) for k in keys] == [100, 600, 100, 500]
    assert int(env["SENTRIX_VLM_REASONING_CONTEXT_TOKENS"]) == 16384
    with urllib.request.urlopen("http://127.0.0.1:18107/v1/models", timeout=30) as response:
        assert [m["id"] for m in json.load(response)["data"]] == ["doubao-seed-2.1-lite"]
    assert min(shutil.disk_usage(p).free for p in ("/home", "/var/tmp")) > 1536 * 1024**2


def stage():
    assert not (HERE / "stage-intent.json").exists() and not CANDIDATE.exists()
    manifest = read("candidate-manifest.json")
    relative = manifest["relative_path"]
    assert sha(OLD / relative) == manifest["baseline_sha256"]
    assert sha(HERE / "writer.patch") == manifest["patch_sha256"]
    assert pid(UNIT) == 351297 and pid(PRODUCT) == 351254
    activity = idle()
    limits()
    save("stage-intent.json", {"created_at": now(), "manifest": manifest, "activity": activity})
    shutil.copytree(OLD, CANDIDATE, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    applied = subprocess.run(
        ["patch", "--batch", "--fuzz=0", "-p1", "-i", str(HERE / "writer.patch")],
        cwd=CANDIDATE,
        text=True,
        capture_output=True,
        check=True,
    )
    assert sha(CANDIDATE / relative) == manifest["target_sha256"]
    changed = []
    for source in OLD.rglob("*"):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        name = source.relative_to(OLD)
        if sha(source) != sha(CANDIDATE / name):
            changed.append(str(name))
    assert changed == [relative], changed
    save(
        "stage-response.json",
        {
            "created_at": now(),
            "changed_files": changed,
            "target_sha256": sha(CANDIDATE / relative),
            "patch_output": applied.stdout,
        },
    )
    print(json.dumps({"staged": True, "changed_files": changed}), flush=True)


def validate():
    from photobench.infrastructure.results.split_store import SplitResultWriter, SplitRunReader

    assert not (HERE / "candidate-validation-intent.json").exists()
    assert read("stage-response.json")["target_sha256"] == read("candidate-manifest.json")["target_sha256"]
    assert read("profile-acceptance.json")["accepted"]
    state = load_source()
    plan = read("inventory.json")
    target = HERE / "candidate-validation" / RUN
    assert not target.exists()
    save(
        "candidate-validation-intent.json",
        {"created_at": now(), "source_manifest_sha256": plan["source_manifest_sha256"]},
    )
    writer = SplitResultWriter(ROOT)
    receipts = []
    old_views = SplitRunReader(ROOT, BASE / "doubao-persist-20261121-v1/isolated-checkpoint" / RUN).manifest[
        "views"
    ]
    for index in range(2):
        profiler = cProfile.Profile()
        began = time.monotonic()
        profiler.enable()
        receipt = writer.write(target, state)
        profiler.disable()
        output = io.StringIO()
        pstats.Stats(profiler, stream=output).strip_dirs().sort_stats("cumulative").print_stats(35)
        (HERE / f"candidate-write-{index}-profile.txt").write_text(output.getvalue())
        saved = SplitRunReader(ROOT, target)
        assert saved.manifest["views"] == old_views, "read models differ from accepted baseline"
        assert fingerprint(saved.state()["items"]) == plan["items_fingerprint"]
        assert fingerprint(saved.state()["scoring_results"]) == plan["scoring_results_fingerprint"]
        assert sha(ROOT / RUN / "split-commit.json") == plan["source_manifest_sha256"]
        receipts.append({"seconds": time.monotonic() - began, "receipt": receipt})
    assert writer._objects is not None
    writer._objects.close()
    baseline = read("profile-acceptance.json")["writes"]
    assert receipts[1]["receipt"]["seconds"] < baseline[1]["receipt"]["seconds"], (
        "no measured warm improvement"
    )
    result = {
        "accepted": True,
        "writes": receipts,
        "baseline_writes": baseline,
        "source_manifest_unchanged": True,
        "all_items_scores_and_read_models_unchanged": True,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "finished_at": now(),
    }
    save("candidate-validation-response.json", result)
    print(json.dumps(result), flush=True)


def activate():
    assert not (HERE / "activate-intent.json").exists()
    assert read("candidate-validation-response.json")["accepted"]
    manifest = read("candidate-manifest.json")
    assert sha(CANDIDATE / manifest["relative_path"]) == manifest["target_sha256"]
    activity = idle()
    limits()
    old_pid = pid(UNIT)
    assert old_pid == 351297
    args = command(old_pid)
    assert args[args.index("--app-dir") + 1] == str(OLD / "apps/photobench/src")
    before_env = environment(old_pid)
    protected = {unit: pid(unit) for unit in PROTECTED}
    source_sha = sha(ROOT / RUN / "split-commit.json")
    mapping_sha = sha(BASE / "active-runs.json")
    folder = Path.home() / ".config/systemd/user" / (UNIT + ".d")
    override = folder / "zzzzzzzzzzzzzzzz-persist-projection-20261121.conf"
    assert not override.exists() and override.name > max(p.name for p in folder.glob("*.conf"))
    args[args.index("--app-dir") + 1] = str(APP)
    save(
        "activate-intent.json",
        {
            "created_at": now(),
            "activity": activity,
            "old_pid": old_pid,
            "protected_pids": protected,
            "source_manifest_sha256": source_sha,
            "mapping_sha256": mapping_sha,
            "override": str(override),
            "command": args,
        },
    )
    override.write_text("[Service]\nExecStart=\nExecStart=" + shlex.join(args) + "\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    for attempt in range(60):
        try:
            api(8874, "active-work")
            break
        except OSError:
            if attempt == 59:
                raise
            time.sleep(1)
    current_pid = pid(UNIT)
    assert current_pid != old_pid
    assert command(current_pid)[command(current_pid).index("--app-dir") + 1] == str(APP)
    after_env = environment(current_pid)
    for key in (
        "PHOTOBENCH_WEB_DIST_ROOT",
        "PHOTOBENCH_PERSIST_TIMEOUT_SECONDS",
        "BENCH_SENTRIX_URL",
        "BENCH_VLLM_BASE_URL",
    ):
        assert after_env.get(key) == before_env.get(key), key
    assert {unit: pid(unit) for unit in PROTECTED} == protected
    assert sha(ROOT / RUN / "split-commit.json") == source_sha
    assert sha(BASE / "active-runs.json") == mapping_sha
    activity_after = idle()
    limits()
    save(
        "activate-response.json",
        {
            "created_at": now(),
            "pid": current_pid,
            "app_dir": str(APP),
            "protected_pids": protected,
            "only_restarted_port": 8874,
            "source_manifest_unchanged": True,
            "environment_unchanged": True,
            "target_sha256": manifest["target_sha256"],
            "activity_after": activity_after,
        },
    )
    print(json.dumps({"pid": current_pid, "only_restarted_port": 8874}), flush=True)


class ExistingTurn:
    def __init__(self, qa, fact, question, scope):
        self.qa, self.fact, self.question, self.scope = qa, fact, question, scope

    def start_conversation(self, *, title, scope_id):
        assert title == self.qa and scope_id == self.scope
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


def seed():
    from photobench.application.run_execution.service import RunExecution, RunRequest
    from photobench.domain.runs.execution import index_assets_by_name
    from photobench.infrastructure.results.split_store import SplitResultWriter, SplitRunReader

    assert not (HERE / "restore-intent.json").exists() and not (ROOT / SEED).exists()
    activation = read("activate-response.json")
    assert pid(UNIT) == activation["pid"]
    idle()
    state = load_source()
    plan = read("inventory.json")
    rows = {q["qa_id"]: q for q in state["qa_snapshot"]["rows"]}
    existing = {i["qa_id"] for i in state["items"]}
    facts = plan["uncollected_product_turns"]
    assert len(facts) == 3 and not existing.intersection(f["qa_id"] for f in facts)
    save(
        "restore-intent.json",
        {
            "source_run": RUN,
            "seed_run": SEED,
            "facts": facts,
            "source_manifest_sha256": plan["source_manifest_sha256"],
            "product_http_methods": ["GET"],
            "agent_rerun": False,
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
        client = ExistingTurn(qa, fact, rows[qa]["question"], state["scope_id"])
        execution = RunExecution(
            client=client, albums=None, writer=None, lifecycle=None, inference=None, telemetry=None
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
        assert item["execution_status"] == "completed", item.get("error")
        assert hashlib.sha256(item["answer"].encode()).hexdigest() == fact["answer_sha256"]
        assert item["runtime_turns"][0]["agent_status"] == fact["status"]
        if fact["status"] == "failed":
            item.update(
                execution_status="failed", error="saved product turn failed: " + fact["termination_reason"]
            )
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
    order = {q: index for index, q in enumerate(rows)}
    state["items"].sort(key=lambda item: order[item["qa_id"]])
    state.update(run_id=SEED, status="failed", current_phase=None, resumed_from=RUN)
    state["terminal_turn_recovery"] = {"source_run_id": RUN, "turns": restored}
    source_items = {i["qa_id"]: i for i in state["items"] if i["qa_id"] in existing}
    assert all(fingerprint(source_items[q]) == value for q, value in plan["item_fingerprints"].items())
    receipt = SplitResultWriter(ROOT).write(ROOT / SEED, state)
    verified = SplitRunReader(ROOT, ROOT / SEED).state()
    assert len(verified["items"]) == 890
    assert fingerprint(verified["scoring_results"]) == plan["scoring_results_fingerprint"]
    assert sha(ROOT / RUN / "split-commit.json") == plan["source_manifest_sha256"]
    remaining = set(rows) - {i["qa_id"] for i in verified["items"]}
    assert remaining == set(plan["never_started_qa_ids"]) and len(remaining) == 123
    save(
        "restore-response.json",
        {
            "seed_run_id": SEED,
            "items": 890,
            "remaining": 123,
            "restored": restored,
            "write": receipt,
            "source_manifest_unchanged": True,
            "all_887_items_and_812_valid_scores_unchanged": True,
        },
    )
    print(json.dumps({"seed_run_id": SEED, "items": 890, "remaining": 123}), flush=True)


def resume(wait_id):
    uuid.UUID(wait_id)
    assert not (HERE / "resume-intent.json").exists()
    idle()
    limits()
    load_source()
    assert read("restore-response.json")["remaining"] == 123
    assert pid(UNIT) == read("activate-response.json")["pid"]
    plan = api(8874, f"runs/{SEED}/recovery-plan")
    assert plan["can_resume"] and plan["inherited_qa_count"] == 890 and plan["remaining_qa_count"] == 123
    assert not plan["product_version_changed"]
    assert plan["frozen_scoring_profiles"] == ["atm_qs_v1"] and not plan["judge_use_gt_images"]
    mapping = read_mapping()
    payload = {"repair_missing_judges": False}
    save(
        "resume-intent.json",
        {
            "wait_id": wait_id,
            "created_at": now(),
            "failed_run": RUN,
            "source_run": SEED,
            "recovery_plan": plan,
            "payload": payload,
            "remaining_qa_ids": read("inventory.json")["never_started_qa_ids"],
        },
    )
    result = api(8874, f"runs/{SEED}/resume", payload)
    save("resume-response.json", result)
    assert result["resumed_from"] == SEED and result["resumed_items"] == 890
    assert result["scope_id"] == read("inventory.json")["scope_id"] and not result["retried_qa_ids"]
    assert read_mapping() == mapping
    mapping["doubao"] = result
    temporary = HERE / "active-runs.next.json"
    temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
    temporary.replace(BASE / "active-runs.json")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("stage", "validate", "activate", "seed", "resume"))
    parser.add_argument("--wait-id")
    args = parser.parse_args()
    sys.path.insert(0, str(APP))
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    if args.mode == "resume":
        assert args.wait_id
        resume(args.wait_id)
    else:
        {"stage": stage, "validate": validate, "activate": activate, "seed": seed}[args.mode]()
