"""Switch only the reviewed, paused limited candidate to a committed release."""

from __future__ import annotations

import argparse
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
OLD_RUN = "20261126-043228-2026.11.17-atm-sources-addresses-big_model-790fec"


def wait_for(check, seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Inspect the saved deployment stage before continuing")
        time.sleep(1)


def run(revision):
    output = BASE / "limited-v2"
    output.mkdir()  # A prior intent requires inspection, not replay.
    release = BASE / f"release-{revision[:8]}"
    assert json.loads((release / "build-info.json").read_text())["revision"] == revision
    manifest = json.loads((release / "source-sha256.json").read_text())
    assert manifest and all(h.sha(release / name) == digest for name, digest in manifest.items())
    snapshot = h.api(8878, f"runs/{OLD_RUN}?include_history=false")
    progress = snapshot["phases"]["qa_eval"]["progress"]
    assert progress["agent_completed"] == 8 and progress["dispatch_paused"]
    assert snapshot.get("fatal_error") is None
    protected = candidate.protected_processes()
    for role in ("api", "eval"):
        pid = subprocess.check_output(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", candidate.unit_name("large-" + role)],
            text=True,
        ).strip()
        protected[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
    data = BASE / "limited/data"
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
            "previous_run": OLD_RUN,
            "at": time.time(),
            "protected": protected,
            "scope": "limited only; preserve eight prior answers, album and large candidate",
        },
    )
    h.save(output / "before-run.json", snapshot)
    h.save(output / "cancel-dispatch.json", h.api(8878, f"runs/{OLD_RUN}/cancel", {}))
    wait_for(lambda: h.api(8878, f"runs/{OLD_RUN}?include_history=false")["status"] == "cancelled")
    wait_for(lambda: not h.api(8878, "active-work")["work"])
    h.save(output / "archived-run.json", h.api(8878, f"runs/{OLD_RUN}?include_history=false"))
    assert h.api(8878, f"runs/{OLD_RUN}?include_history=false")["item_count"] == 8

    units = [candidate.unit_name("limited-" + role) for role in ("eval", "api")]
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
        assert str(BASE / "release") in old
        path.write_text(old.replace(str(BASE / "release"), str(release)))
    env_path = BASE / "limited-eval.env"
    (output / "before-eval.env").write_bytes(env_path.read_bytes())
    (output / "before-eval.env").chmod(0o600)
    values = h.load_env(env_path)
    values["PHOTOBENCH_SENTRIX_REPO_ROOT"] = release
    values["PHOTOBENCH_BUILD_REVISION"] = revision
    h.env(env_path, values)
    config_path = BASE / "limited/config/runtime_connection.json"
    config = json.loads(config_path.read_text())
    h.save(output / "before-runtime-connection.json", config)
    assert config["sentrix_url"] == "http://127.0.0.1:8298"
    assert config["product_build_revision"] == "7fbb4ed85b6bde8d71d0f7ced1a7eab073c721dc"
    config["product_build_revision"] = revision
    h.save(config_path, config)
    h.save(output / "configured.json", {"revision": revision, "at": time.time()})
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", *reversed(units)], check=True)

    def healthy():
        try:
            return h.api(8298, "health/live")["build_revision"] == revision
        except OSError:
            return False

    wait_for(healthy)

    def configured():
        try:
            return h.api(8878, "config")["runtime_config"]["product_build_revision"] == revision
        except OSError:
            return False

    wait_for(configured)
    diagnostics = h.api(8298, "diagnostics")
    assert diagnostics["build_revision"] == revision
    assert diagnostics["database"] == "ok" and diagnostics["embeddings"] == "ok"
    actual = diagnostics["runtime_configuration"]
    for key, value in {
        "reasoning_max_model_calls": 20,
        "reasoning_max_tool_calls": 30,
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
            "files_verified": len(manifest),
            "diagnostics": diagnostics,
            "memory_18_tables_unchanged": True,
            "protected_processes_unchanged": protected,
            "old_eight_answers_preserved": True,
            "new_qa_started": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    run(args.revision)
