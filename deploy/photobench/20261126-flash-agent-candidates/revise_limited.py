"""Switch one reviewed, paused candidate while preserving the other instances."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location("candidate_helpers", Path(__file__).with_name("prepare.py"))
candidate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(candidate)
h = candidate.helper
BASE = candidate.BASE
OLD_RUNS = {
    "limited": "20261126-043228-2026.11.17-atm-sources-addresses-big_model-790fec",
    "large": "20261126-043228-2026.11.17-atm-sources-addresses-big_model-2211ea",
}
STAGES = {
    "v7": {
        "runs": {"large": "20261126-075533-2026.11.17-atm-sources-addresses-big_model-120cba"},
        "previous_revision": "3ccfa64adef5542695e75a49e88a51fe5c1f4a8e",
        "previous_release": "release-3ccfa64a",
        "accepted": "v6",
        "answer_count": 83,
        "answer_word": "priority83",
    },
    "v2": {
        "runs": OLD_RUNS,
        "previous_revision": "7fbb4ed85b6bde8d71d0f7ced1a7eab073c721dc",
        "previous_release": "release",
        "accepted": "v1",
        "answer_count": 8,
        "answer_word": "eight",
    },
    "v3": {
        "runs": {"large": "20261126-050817-2026.11.17-atm-sources-addresses-big_model-e9700d"},
        "previous_revision": "11deb1c68068015b1ad31e10579dbd7188707d20",
        "previous_release": "release-11deb1c6",
        "accepted": "v2",
        "answer_count": 8,
        "answer_word": "eight",
    },
    "v4": {
        "runs": {"large": "20261126-054519-2026.11.17-atm-sources-addresses-big_model-36e9da"},
        "previous_revision": "f60aba028e7fc9107638d6f1a603b9b0f76200ae",
        "previous_release": "release-f60aba02",
        "accepted": "v3",
        "answer_count": 11,
        "answer_word": "eleven",
    },
    "v5": {
        "runs": {"large": "20261126-064511-2026.11.17-atm-sources-addresses-big_model-f4fd18"},
        "previous_revision": "4bc53cad92f31b93859f9de3d90d52ee53a2783f",
        "previous_release": "release-4bc53cad",
        "accepted": "v4",
        "answer_count": 11,
        "answer_word": "eleven",
    },
    "v6": {
        "runs": {"large": "20261126-070857-2026.11.17-atm-sources-addresses-big_model-e0beaf"},
        "previous_revision": "871fbef465100c8461b9cb18af91601803437d6b",
        "previous_release": "release-871fbef4",
        "accepted": "v5",
        "answer_count": 11,
        "answer_word": "eleven",
    },
}


def wait_for(check, seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Inspect the saved deployment stage before continuing")
        time.sleep(1)


def start_archive(revision, *, group="limited", stage="v2"):
    product, evaluation, _models, _tools = candidate.GROUPS[group]
    old_run = STAGES[stage]["runs"][group]
    output = BASE / f"{group}-{stage}"
    output.mkdir()  # A prior intent requires inspection, not replay.
    release = BASE / f"release-{revision[:8]}"
    assert json.loads((release / "build-info.json").read_text())["revision"] == revision
    manifest = json.loads((release / "source-sha256.json").read_text())
    assert manifest and all(h.sha(release / name) == digest for name, digest in manifest.items())
    snapshot = h.api(evaluation, f"runs/{old_run}?include_history=false")
    progress = snapshot["phases"]["qa_eval"]["progress"]
    assert progress["agent_completed"] == STAGES[stage]["answer_count"] and progress["dispatch_paused"]
    assert snapshot.get("fatal_error") is None
    protected = candidate.protected_processes()
    for role in ("api", "eval"):
        pid = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "MainPID",
                "--value",
                candidate.unit_name(("large" if group == "limited" else "limited") + "-" + role),
            ],
            text=True,
        ).strip()
        protected[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
    data = BASE / group / "data"
    with sqlite3.connect(f"file:{data}/sentrix.db?mode=ro", uri=True) as db:
        active = db.execute(
            "select count(*) from agent_turns where status in ('pending','running')"
        ).fetchone()[0]
        assert active == 0
    h.save(
        output / "intent.json",
        {
            "revision": revision,
            "release": str(release),
            "previous_run": old_run,
            "stage": stage,
            "at": time.time(),
            "protected": protected,
            "scope": (
                f"{group} only; preserve {STAGES[stage]['answer_count']} prior answers, "
                "album and other candidate"
            ),
        },
    )
    h.save(output / "before-run.json", snapshot)
    h.save(output / "cancel-dispatch.json", h.api(evaluation, f"runs/{old_run}/cancel", {}))
    wait_for(lambda: h.api(evaluation, f"runs/{old_run}?include_history=false")["status"] == "cancelled")
    wait_for(lambda: not h.api(evaluation, "active-work")["work"])
    h.save(output / "archived-run.json", h.api(evaluation, f"runs/{old_run}?include_history=false"))
    return output, release, protected


def verify_archived_answers(output, *, group="limited", stage="v2"):
    evaluation = candidate.GROUPS[group][1]
    old_run = STAGES[stage]["runs"][group]
    snapshot = h.api(evaluation, f"runs/{old_run}?include_history=false")
    assert snapshot["status"] == "cancelled"
    assert snapshot["phases"]["qa_eval"]["progress"]["agent_completed"] == STAGES[stage]["answer_count"]
    accepted = json.loads(
        (
            BASE / f"{group}-{STAGES[stage]['accepted']}-accepted-{STAGES[stage]['answer_word']}.json"
        ).read_text()
    )
    assert len(accepted) == STAGES[stage]["answer_count"]
    for qa_id, digest in accepted.items():
        rows = h.api(evaluation, f"runs/{old_run}/items?search={qa_id}&page=1&page_size=10")["items"]
        assert len(rows) == 1 and rows[0]["qa_id"] == qa_id
        item = h.api(evaluation, f"runs/{old_run}/items/{rows[0]['index']}")["item"]
        assert hashlib.sha256(item["answer"].encode()).hexdigest() == digest
    h.save(
        output / "answer-preservation.json",
        {
            "verified_answers": len(accepted),
            "answer_sha256": accepted,
            "item_count": snapshot["item_count"],
            "unexecuted_rows": snapshot["item_count"] - len(accepted),
        },
    )


def run(revision, *, group="limited", stage="v2", continue_archived=False):
    product, evaluation, models, tools = candidate.GROUPS[group]
    if continue_archived:
        output = BASE / f"{group}-{stage}"
        intent = json.loads((output / "intent.json").read_text())
        assert intent["revision"] == revision
        assert not (output / "configured.json").exists()
        assert not h.api(evaluation, "active-work")["work"]
        release = Path(intent["release"])
        protected = intent["protected"]
        assert all(
            Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in protected.items()
        )
        manifest = json.loads((release / "source-sha256.json").read_text())
        assert all(h.sha(release / name) == digest for name, digest in manifest.items())
    else:
        output, release, protected = start_archive(revision, group=group, stage=stage)
    verify_archived_answers(output, group=group, stage=stage)
    data = BASE / group / "data"
    units = [candidate.unit_name(group + "-" + role) for role in ("eval", "api")]
    subprocess.run(["systemctl", "--user", "stop", *units], check=True)
    source = json.loads((h.SNAPSHOT / "memory-fingerprints.json").read_text())
    with sqlite3.connect(f"file:{data}/sentrix.db?mode=ro", uri=True) as db:
        before = h.memory_fingerprints(db, source, data)
    assert before == source
    h.save(output / "memory-before.json", before)
    root = Path.home() / ".config/systemd/user"
    for unit in units:
        path = root / unit
        old = path.read_text()
        (output / unit).write_text(old)
        previous_release = str(BASE / STAGES[stage]["previous_release"])
        assert previous_release in old
        path.write_text(old.replace(previous_release, str(release)))
    env_path = BASE / f"{group}-eval.env"
    (output / "before-eval.env").write_bytes(env_path.read_bytes())
    (output / "before-eval.env").chmod(0o600)
    values = h.load_env(env_path)
    values["PHOTOBENCH_SENTRIX_REPO_ROOT"] = release
    values["PHOTOBENCH_BUILD_REVISION"] = revision
    h.env(env_path, values)
    config_path = BASE / group / "config/runtime_connection.json"
    config = json.loads(config_path.read_text())
    h.save(output / "before-runtime-connection.json", config)
    assert config["sentrix_url"] == f"http://127.0.0.1:{product}"
    assert config["product_build_revision"] == STAGES[stage]["previous_revision"]
    config["product_build_revision"] = revision
    h.save(config_path, config)
    h.save(output / "configured.json", {"revision": revision, "at": time.time()})
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", *reversed(units)], check=True)

    def healthy():
        try:
            return h.api(product, "health/live")["build_revision"] == revision
        except OSError:
            return False

    wait_for(healthy)

    def configured():
        try:
            return h.api(evaluation, "config")["runtime_config"]["product_build_revision"] == revision
        except OSError:
            return False

    wait_for(configured)
    diagnostics = h.api(product, "diagnostics")
    assert diagnostics["build_revision"] == revision
    assert diagnostics["database"] == "ok" and diagnostics["embeddings"] == "ok"
    actual = diagnostics["runtime_configuration"]
    for key, value in {
        "reasoning_max_model_calls": models,
        "reasoning_max_tool_calls": tools,
        "reasoning_max_search_calls": 100,
        "reasoning_max_other_tool_calls": 500,
        "agent_turn_workers": 2,
        "reasoning_max_steps": 100,
        "vlm_context_tokens": 131072,
        "vlm_model": "qwen3.8-flash",
    }.items():
        assert actual[key] == value, (key, actual[key])
    with sqlite3.connect(f"file:{data}/sentrix.db?mode=ro", uri=True) as db:
        assert h.memory_fingerprints(db, source, data) == before
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in protected.items())
    h.save(
        output / "verification.json",
        {
            "revision": revision,
            "at": time.time(),
            "files_verified": len(json.loads((release / "source-sha256.json").read_text())),
            "diagnostics": diagnostics,
            "memory_18_tables_unchanged": True,
            "protected_processes_unchanged": protected,
            "old_answers_preserved": STAGES[stage]["answer_count"],
            "new_qa_started": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--group", choices=["limited", "large"], default="limited")
    parser.add_argument("--stage", choices=STAGES, default="v2")
    parser.add_argument("--continue-archived", action="store_true")
    args = parser.parse_args()
    run(args.revision, group=args.group, stage=args.stage, continue_archived=args.continue_archived)
