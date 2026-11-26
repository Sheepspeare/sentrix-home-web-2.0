"""Prepare one independent v7 instance while the v6 comparison keeps running."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-query-v7-parallel-20261126")
PREFIX = "sentrix-flash-query-v7"
PORTS = (8300, 8880, 100, 600)


def candidate_module(release):
    path = release / "deploy/photobench/20261126-flash-agent-candidates/prepare.py"
    spec = importlib.util.spec_from_file_location("parallel_candidate_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASE = BASE
    module.PREFIX = PREFIX
    module.GROUPS = {"large": PORTS}
    return module


def payload_for_parallel(payload):
    result = dict(payload)
    result["sentrix_url"] = f"http://127.0.0.1:{PORTS[0]}"
    result["qa_concurrency_control_path"] = str(BASE / "large/dispatch-control.json")
    return result


def prepare(release, payload_path):
    p = candidate_module(release)
    h = p.helper
    revision = json.loads((release / "build-info.json").read_text())["revision"]
    manifest = json.loads((release / "source-sha256.json").read_text())
    assert all(h.sha(release / name) == digest for name, digest in manifest.items())
    assert not BASE.exists(), "Inspect the existing parallel instance; do not replay preparation"
    assert shutil.disk_usage(BASE.parent).free > (h.SNAPSHOT / "sentrix.db").stat().st_size + 5 * 1024**3 / 2
    p.check_ports()
    protected = p.protected_processes()
    for name in (
        "sentrix-flash-dual-gateway",
        "sentrix-flash-candidates-limited-api",
        "sentrix-flash-candidates-limited-eval",
        "sentrix-flash-candidates-large-api",
        "sentrix-flash-candidates-large-eval",
    ):
        pid = subprocess.check_output(
            ["systemctl", "--user", "show", name + ".service", "--property=MainPID", "--value"],
            text=True,
        ).strip()
        assert int(pid) > 0
        protected[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
    for role in ("api", "eval"):
        assert not (Path.home() / ".config/systemd/user" / p.unit_name("large-" + role)).exists()
    payload = json.loads(payload_path.read_text())
    assert payload["qa_concurrency"] == 2 and payload["defer_scoring"] is True
    assert payload["reasoning"] is True and len(set(payload["qa_priority_ids"])) == 83
    BASE.mkdir()
    h.save(BASE / "intent.json", {"revision": revision, "protected": protected, "at": time.time()})
    (BASE / "release").symlink_to(release, target_is_directory=True)
    shutil.copy2(p.BASELINE / "model-profile.json", BASE / "model-profile.json")
    source = json.loads((h.SNAPSHOT / "memory-fingerprints.json").read_text())
    _target, data = p.clone_album("large", source)
    p.configure_group("large", PORTS, revision, data)
    h.save(BASE / "large/dispatch-control.json", {"concurrency": 2, "pause_after_completed": 16})
    h.save(BASE / "large/run-payload.json", payload_for_parallel(payload))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for role in ("api", "eval"):
        subprocess.run(["systemctl", "--user", "start", p.unit_name("large-" + role)], check=True)
    for _ in range(60):
        try:
            live = h.api(PORTS[0], "health/live")
            config = h.api(PORTS[1], "config")
            assert live["build_revision"] == revision
            assert config["runtime_config"]["product_build_revision"] == revision
            break
        except (OSError, AssertionError):
            time.sleep(1)
    else:
        raise TimeoutError("Inspect the independent service logs before continuing")
    diagnostics = h.api(PORTS[0], "diagnostics")
    settings = diagnostics["runtime_configuration"]
    for key, expected in {
        "reasoning_max_steps": 100,
        "reasoning_max_model_calls": 100,
        "reasoning_max_tool_calls": 600,
        "agent_turn_workers": 2,
        "vlm_reasoning_context_tokens": 131072,
    }.items():
        assert settings[key] == expected, (key, settings[key])
    assert diagnostics["database"] == "ok" and diagnostics["embeddings"] == "ok"
    with sqlite3.connect(f"file:{data}/sentrix.db?mode=ro", uri=True) as db:
        assert h.memory_fingerprints(db, source, data) == source
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in protected.items())
    h.save(
        BASE / "prepared.json",
        {
            "revision": revision,
            "ports": PORTS[:2],
            "memory_18_tables_unchanged": True,
            "protected_processes_unchanged": protected,
            "diagnostics": diagnostics,
            "new_embedding_requests": 0,
            "new_qa_started": False,
            "at": time.time(),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.release, args.payload)
    except BaseException as error:
        if BASE.exists():
            (BASE / "prepare-error.json").write_text(
                json.dumps({"type": type(error).__name__, "error": str(error), "at": time.time()})
            )
        raise
