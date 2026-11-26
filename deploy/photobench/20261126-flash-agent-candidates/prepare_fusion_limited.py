"""Prepare an isolated 20/30 comparison using the accepted fusion product."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-condition-fusion-limited-20261127")
PREFIX = "sentrix-flash-condition-fusion-limited83"
PORTS = (8304, 8884, 20, 30)
REVISION = "bc73615327e62f5dcd60ae018a61aae883759ad8"
WORKERS = 8


def validate_payload(payload: dict, reference_ids: list[str]) -> None:
    assert len(reference_ids) == len(set(reference_ids)) == 83
    assert payload["qa_priority_ids"] == reference_ids
    assert payload["sentrix_url"] == f"http://127.0.0.1:{PORTS[0]}"
    assert payload["qa_concurrency"] == WORKERS
    assert payload["reasoning"] is True and payload["defer_scoring"] is True
    assert payload["mode"] == "reuse" and payload["endpoint_model"] == "qwen3.8-flash"
    assert payload["qa_concurrency_control_path"] == str(BASE / "limited/dispatch-control.json")


def run(release: Path, payload_path: Path, ids_path: Path) -> None:
    sys.path.insert(0, str(release / "deploy/photobench/20261126-flash-agent-candidates"))
    import prepare as candidate
    import prepare_fusion
    import prepare_parallel

    helper = candidate.helper
    candidate.BASE = BASE
    candidate.PREFIX = PREFIX
    candidate.GROUPS = {"limited": PORTS}
    candidate.WORKERS = WORKERS
    payload = json.loads(payload_path.read_text())
    ids = json.loads(ids_path.read_text())
    validate_payload(payload, ids)
    assert json.loads((release / "build-info.json").read_text())["revision"] == REVISION
    manifest = json.loads((release / "source-sha256.json").read_text())
    assert len(manifest) == 388
    assert all(helper.sha(release / name) == digest for name, digest in manifest.items())
    assert not BASE.exists(), "Inspect existing stage receipts instead of repeating preparation"
    assert shutil.disk_usage(BASE.parent).free > (helper.SNAPSHOT / "sentrix.db").stat().st_size + 3 * 1024**3
    candidate.check_ports()
    for role in ("api", "eval"):
        assert not (Path.home() / ".config/systemd/user" / candidate.unit_name("limited-" + role)).exists()
    protected = prepare_fusion.protected_processes(candidate.protected_processes)
    BASE.mkdir()
    helper.save(
        BASE / "intent.json",
        {
            "at": time.time(),
            "product_revision": REVISION,
            "protected": protected,
            "ports": PORTS,
            "qa_concurrency": WORKERS,
            "checkpoint": 83,
            "reference_qa_ids": ids,
            "snapshot": str(helper.SNAPSHOT),
            "new_qa_started": False,
        },
    )
    prepare_parallel.compose_release(
        release, candidate.BASELINE / "release/apps/photobench", BASE / "release"
    )
    shutil.copy2(candidate.BASELINE / "model-profile.json", BASE / "model-profile.json")
    source = json.loads((helper.SNAPSHOT / "memory-fingerprints.json").read_text())
    assert len(source) == 18
    _, data = candidate.clone_album("limited", source)
    candidate.configure_group("limited", PORTS, REVISION, data)
    helper.save(BASE / "limited/dispatch-control.json", {"concurrency": WORKERS, "pause_after_completed": 83})
    helper.save(BASE / "limited/run-payload.json", payload)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for role in ("api", "eval"):
        subprocess.run(["systemctl", "--user", "start", candidate.unit_name("limited-" + role)], check=True)
    for _ in range(90):
        try:
            live = helper.api(PORTS[0], "health/live")
            config = helper.api(PORTS[1], "config")
            assert live["build_revision"] == REVISION
            assert config["runtime_config"]["product_build_revision"] == REVISION
            break
        except (OSError, AssertionError):
            time.sleep(1)
    else:
        raise TimeoutError("Inspect the new limited service logs before continuing")
    diagnostics = helper.api(PORTS[0], "diagnostics")
    settings = diagnostics["runtime_configuration"]
    for name, expected in {
        "reasoning_max_model_calls": 20,
        "reasoning_max_tool_calls": 30,
        "reasoning_max_steps": 100,
        "agent_turn_workers": WORKERS,
        "vlm_reasoning_context_tokens": 131072,
    }.items():
        assert settings[name] == expected, (name, settings[name])
    assert diagnostics["database"] == "ok" and diagnostics["embeddings"] == "ok"
    with sqlite3.connect(f"file:{data}/sentrix.db?mode=ro", uri=True) as database:
        assert helper.memory_fingerprints(database, source, data) == source
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in protected.items())
    helper.save(
        BASE / "prepared.json",
        {
            "at": time.time(),
            "revision": REVISION,
            "files_verified": len(manifest),
            "product_source": str(release),
            "ports": PORTS[:2],
            "memory_18_tables_unchanged": True,
            "protected_processes_unchanged": protected,
            "diagnostics": diagnostics,
            "new_embedding_requests": 0,
            "new_qa_started": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--ids", required=True, type=Path)
    options = parser.parse_args()
    try:
        run(options.release, options.payload, options.ids)
    except Exception as error:
        if BASE.exists():
            (BASE / "prepare-error.json").write_text(
                json.dumps({"at": time.time(), "type": type(error).__name__, "error": str(error)})
            )
        raise
