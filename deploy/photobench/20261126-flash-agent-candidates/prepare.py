"""Prepare isolated candidate instances using the accepted album and existing gateway."""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

_HELPER = Path(__file__).parents[1] / "20261126-flash-dual-baseline/operate.py"
_spec = importlib.util.spec_from_file_location("flash_baseline_helpers", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-agent-candidates-20261126")
GROUPS = {"limited": (8298, 8878, 20, 30), "large": (8299, 8879, 100, 600)}
BASELINE = BASE.with_name("flash-dual-baseline-20261126")
PREFIX = "sentrix-flash-candidates"
WORKERS = 2


def unit_name(name):
    return f"{PREFIX}-{name}.service"


def unit(name, values, port, module, appdir, secrets=False):
    environment = BASE / (name + ".env")
    helper.env(environment, values)
    text = (
        f"[Unit]\nDescription=Flash candidate {name}\n[Service]\n"
        f"WorkingDirectory={BASE}/release\nEnvironmentFile={environment}\n"
    )
    if secrets:
        text += f"EnvironmentFile={helper.OLD}/secrets.env\n"
    text += (
        f"ExecStart={helper.PYTHON} -m uvicorn --factory {module}:create_app --host 0.0.0.0 "
        f"--port {port} --app-dir {appdir}\nRestart=on-failure\nRestartSec=5\n"
        f"StandardOutput=append:{BASE}/{name}.log\nStandardError=append:{BASE}/{name}.log\n"
    )
    destination = Path.home() / ".config/systemd/user" / unit_name(name)
    assert not destination.exists(), destination
    destination.write_text(text)
    destination.chmod(0o600)


def check_ports():
    for product, evaluation, _models, _tools in GROUPS.values():
        for port in (product, evaluation):
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0, f"Port {port} already occupied"


def clone_album(group, source_fingerprints):
    target = BASE / group
    target.mkdir()
    data = target / "data"
    data.mkdir()
    shutil.copy2(helper.SNAPSHOT / "sentrix.db", data / "sentrix.db")
    (data / "sentrix.db").chmod(0o600)
    for directory in ("media", "previews", "video-jobs", "geo"):
        if (helper.SNAPSHOT / directory).exists():
            (data / directory).symlink_to(helper.SNAPSHOT / directory, target_is_directory=True)
    with sqlite3.connect(data / "sentrix.db") as db:
        db.row_factory = sqlite3.Row
        paths = [row[0] for row in db.execute("select path from assets")]
        assert len(paths) == 13356
        assert all(path.startswith(helper.ORIGINAL_DATA + "/") for path in paths)
        db.execute("update assets set path=? || substr(path,?)", (str(data), len(helper.ORIGINAL_DATA) + 1))
        db.commit()
        assert all(Path(row[0]).is_file() for row in db.execute("select path from assets"))
        # The frozen album must not resume historical mutation jobs in a QA clone.
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        for table in ("agent_turns", "import_jobs", "video_jobs", "pipeline_receipts"):
            if table in tables:
                statuses = {str(row[0]).lower() for row in db.execute(f"select distinct status from {table}")}
                assert not statuses & {
                    "accepted",
                    "queued",
                    "running",
                    "processing",
                    "retrying",
                    "pending",
                    "retry_wait",
                }, (table, statuses)
        if "media_cleanup_jobs" in tables:
            assert db.execute("select count(*) from media_cleanup_jobs").fetchone()[0] == 0
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
        fingerprints = helper.memory_fingerprints(db, source_fingerprints, data)
        assert fingerprints == source_fingerprints, "Clone changed frozen memory"
    helper.save(
        target / "clone-verification.json",
        {
            "snapshot_db_sha256": helper.sha(helper.SNAPSHOT / "sentrix.db"),
            "mapped_paths": len(paths),
            "memory_fingerprints": fingerprints,
            "memory_matches_snapshot_after_path_normalization": True,
            "media": "read-only snapshot shared by symlink",
        },
    )
    build_album_cache(group, data)
    return target, data


def cache_path(group):
    return Path("/dev/shm") / f"sentrix-{BASE.name}-{group}"


def build_album_cache(group, data):
    cache = cache_path(group)
    cache.mkdir(mode=0o700)
    (data / "qdrant").symlink_to(cache, target_is_directory=True)
    helper.BASE = BASE
    helper.build_cache(data)


def configure_group(group, ports, revision, data):
    product, evaluation, models, tools = ports
    target = BASE / group
    config = target / "config"
    shutil.copytree(BASELINE / group / "config", config)
    connection = json.loads((BASELINE / group / "config/runtime_connection.json").read_text())
    connection.update(
        sentrix_url=f"http://127.0.0.1:{product}",
        model_base_url=helper.GATEWAY,
        product_build_revision=revision,
    )
    settings = connection["product_settings"]
    assert settings["reasoning_max_model_calls"] == models
    assert settings["reasoning_max_tool_calls"] == tools
    settings["agent_turn_workers"] = WORKERS
    helper.save(config / "runtime_connection.json", connection)
    for folder in ("results", "runtime"):
        (target / folder).mkdir()
    product_env = helper.load_env(BASELINE / f"{group}-api.env")
    assert product_env["SENTRIX_VLM_BASE_URL"] == helper.GATEWAY
    assert int(product_env["SENTRIX_REASONING_MAX_MODEL_CALLS"]) == models
    assert int(product_env["SENTRIX_REASONING_MAX_TOOL_CALLS"]) == tools
    product_env.update(
        SENTRIX_DATA_DIR=data,
        SENTRIX_API_PORT=product,
        SENTRIX_LLM_CONFIG=BASE / "model-profile.json",
        SENTRIX_AGENT_TURN_MAX_WORKERS=WORKERS,
    )
    unit(group + "-api", product_env, product, "sentrix.main", BASE / "release/apps/api/src", True)
    evaluation_env = helper.load_env(BASELINE / f"{group}-eval.env")
    evaluation_env.update(
        PHOTOBENCH_CONFIG_ROOT=config,
        PHOTOBENCH_RESULTS_ROOT=target / "results",
        PHOTOBENCH_SENTRIX_API_URL=f"http://127.0.0.1:{product}",
        BENCH_SENTRIX_URL=f"http://127.0.0.1:{product}",
        BENCH_VLLM_BASE_URL=helper.GATEWAY,
        PHOTOBENCH_SENTRIX_REPO_ROOT=BASE / "release",
        PHOTOBENCH_SENTRIX_SERVICE_UNIT=unit_name(group + "-api"),
        PHOTOBENCH_SENTRIX_START_COMMAND=f"systemctl --user start {unit_name(group + '-api')}",
        PHOTOBENCH_SENTRIX_LOG_PATH=BASE / (group + "-api.log"),
        PHOTOBENCH_SENTRIX_PID_FILE=target / "runtime/api.json",
        PHOTOBENCH_BUILD_REVISION=revision,
        PHOTOBENCH_PORT=evaluation,
    )
    unit(group + "-eval", evaluation_env, evaluation, "photobench.main", BASE / "release/apps/photobench/src")
    helper.save(target / "dispatch-control.json", {"concurrency": WORKERS})


def protected_processes():
    pids = {4191434, 2427495, 2427487}
    for group in GROUPS:
        for role in ("api", "eval"):
            pid = subprocess.check_output(
                [
                    "systemctl",
                    "--user",
                    "show",
                    f"sentrix-flash-dual-{group}-{role}.service",
                    "--property=MainPID",
                    "--value",
                ],
                text=True,
            ).strip()
            assert int(pid) > 0, (group, role, pid)
            pids.add(int(pid))
    return {str(pid): Path(f"/proc/{pid}/stat").read_text().split()[21] for pid in pids}


def prepare():
    assert not (BASE / "prepare-intent.json").exists(), "Inspect existing outcome; do not replay prepare"
    # Only SQLite is copied; media stays in the existing immutable snapshot.
    assert shutil.disk_usage(BASE).free > 2 * (helper.SNAPSHOT / "sentrix.db").stat().st_size + 1024**3
    check_ports()
    for group in GROUPS:
        assert not (BASE / group).exists(), f"Inspect existing {group} directory"
        for role in ("api", "eval"):
            assert not (Path.home() / ".config/systemd/user" / unit_name(group + "-" + role)).exists()
    revision = json.loads((BASE / "release/build-info.json").read_text())["revision"]
    manifest = json.loads((BASE / "source-sha256.json").read_text())
    assert manifest and all(
        helper.sha(BASE / "release" / name) == digest for name, digest in manifest.items()
    )
    protected = protected_processes()
    gateway_health_url = helper.GATEWAY.removesuffix("/v1") + "/health"
    with urllib.request.urlopen(gateway_health_url, timeout=15) as response:
        gateway_health = json.load(response)
    assert gateway_health["model"] == "qwen3.8-flash"
    assert gateway_health["enable_thinking"] is False
    helper.save(
        BASE / "prepare-intent.json",
        {
            "at": time.time(),
            "revision": revision,
            "snapshot": str(helper.SNAPSHOT),
            "shared_gateway": helper.GATEWAY,
            "gateway_health": gateway_health,
        },
    )
    shutil.copy2(BASELINE / "model-profile.json", BASE / "model-profile.json")
    source = json.loads((helper.SNAPSHOT / "memory-fingerprints.json").read_text())
    for group, ports in GROUPS.items():
        _target, data = clone_album(group, source)
        configure_group(group, ports, revision, data)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for group in GROUPS:
        for role in ("api", "eval"):
            subprocess.run(["systemctl", "--user", "start", unit_name(group + "-" + role)], check=True)
    health = {}
    for group, (product, evaluation, models, tools) in GROUPS.items():
        last_error = ""
        for _ in range(90):
            try:
                live = helper.api(product, "health/live")
                config = helper.api(evaluation, "config")
                assert live["build_revision"] == revision
                assert config["runtime_config"]["product_build_revision"] == revision
                break
            except Exception as error:
                last_error = str(error)
                time.sleep(2)
        else:
            raise RuntimeError(f"{group} startup failed: {last_error}")
        diagnostics = helper.api(product, "diagnostics")
        settings = diagnostics["runtime_configuration"]
        for key, value in {
            "reasoning_max_steps": 100,
            "reasoning_max_model_calls": models,
            "reasoning_max_tool_calls": tools,
            "reasoning_max_search_calls": 100,
            "reasoning_max_other_tool_calls": 500,
            "agent_turn_workers": WORKERS,
            "vlm_reasoning_context_tokens": 131072,
        }.items():
            assert settings[key] == value, (group, key, settings[key])
        assert diagnostics["database"] == "ok" and diagnostics["embeddings"] == "ok"
        data = BASE / group / "data"
        with sqlite3.connect(data / "sentrix.db") as db:
            db.row_factory = sqlite3.Row
            assert helper.memory_fingerprints(db, source, data) == source
        health[group] = {"live": live, "diagnostics": diagnostics, "post_start_memory_matches_snapshot": True}
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in protected.items())
    helper.save(
        BASE / "prepared.json",
        {
            "at": time.time(),
            "revision": revision,
            "health": health,
            "protected_processes_unchanged": protected,
            "shared_gateway": helper.GATEWAY,
            "new_embedding_requests": 0,
        },
    )


if __name__ == "__main__":
    try:
        prepare()
    except BaseException as error:
        helper.save(
            BASE / "prepare-terminal.json",
            {
                "status": "failed",
                "error": str(error),
                "error_type": type(error).__name__,
                "at": time.time(),
            },
        )
        raise
    helper.save(BASE / "prepare-terminal.json", {"status": "completed", "at": time.time()})
