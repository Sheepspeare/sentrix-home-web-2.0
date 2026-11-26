"""Prepare two isolated instances of the frozen Doubao Agent, with direct cloud I/O."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-doubao-code-direct-20261126")
RAM = Path("/dev/shm/sentrix-doubao-code-direct-20261126")
SOURCE = Path("/home/realmagic/xy/benchmarks/atm-bench/instances/doubao/product-data")
BASELINE = BASE.with_name("flash-dual-baseline-20261126")
CLOUD_CONFIG = BASE.with_name("qwen38-flash-20261124") / "gateway-config.json"
SECRETS = CLOUD_CONFIG.with_name("secrets.env")
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"
SCOPE = "PhotoBench-20261112-174516-atm-bench-media-current"
GROUPS = {"limited": (8301, 8881, 20, 30), "large": (8302, 8882, 100, 600)}
PREFIX = "sentrix-flash-doubao-direct"
TABLES = (
    "assets",
    "observations",
    "events",
    "memory_vectors",
    "text_vectors",
    "facts",
    "entities",
    "entity_properties",
    "relationships",
    "memory_graph_nodes",
    "memory_graph_edges",
    "entity_observations",
    "event_entities",
    "entity_mentions",
    "event_observations",
    "observation_search_terms",
    "observation_search_fts",
    "memory_graph_builds",
)


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def api(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=180) as response:
        return json.load(response)


def load_env(path):
    return {
        k: json.loads(v)
        for k, v in (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    }


def fingerprints(connection):
    result = {}
    for table in TABLES:
        columns = connection.execute(f'pragma table_info("{table}")').fetchall()
        assert columns, table
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = ",".join('"' + key + '"' for key in keys) or "rowid"
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            values = [value.hex() if isinstance(value, bytes) else value for value in row]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
            count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def protected():
    pids = {4191434, 2427495, 2427487}
    units = subprocess.check_output(
        ["systemctl", "--user", "list-units", "--type=service", "--state=running", "--no-legend"], text=True
    )
    for line in units.splitlines():
        name = line.split()[0]
        if name.startswith(("sentrix-flash-dual-", "sentrix-flash-candidates-", "sentrix-flash-query-v7-")):
            pid = int(
                subprocess.check_output(
                    ["systemctl", "--user", "show", name, "-p", "MainPID", "--value"], text=True
                )
            )
            assert pid > 0, name
            pids.add(pid)
    return {str(pid): Path(f"/proc/{pid}/stat").read_text().split()[21] for pid in sorted(pids)}


def verify_protected(before):
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in before.items())


def build_cache(data):
    sys.path.insert(0, str(BASE / "release/apps/api/src"))
    from sentrix.domain.retrieval.ports import VectorRecord
    from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

    with sqlite3.connect(data / "sentrix.db") as db:
        db.row_factory = sqlite3.Row
        index = QdrantVectorIndex(data / "qdrant")
        batch = []
        count = 0
        for row in db.execute("select * from memory_vectors"):
            batch.append(
                VectorRecord(
                    row["scope_id"],
                    row["space"],
                    row["source_type"],
                    row["source_id"],
                    row["model_name"],
                    tuple(json.loads(row["vector_json"])),
                    json.loads(row["metadata_json"]),
                )
            )
            if len(batch) == 128:
                count += index.upsert(batch)
                batch = []
        count += index.upsert(batch)
        expected = db.execute("select count(*) from memory_vectors").fetchone()[0]
        assert count == expected
        counts = {
            (row[0], row[1]): index.count(space=row[0], model_name=row[1])
            for row in db.execute("select distinct space,model_name from memory_vectors")
        }
        assert sum(counts.values()) == count
        checks = []
        for row in db.execute("select * from memory_vectors where scope_id=? order by id limit 3", (SCOPE,)):
            hits = index.search(
                space=row["space"],
                model_name=row["model_name"],
                vector=json.loads(row["vector_json"]),
                scope_id=SCOPE,
                limit=10,
            )
            assert any(hit.source_id == row["source_id"] for hit in hits)
            checks.append(row["source_id"])
        index.close()
    save(
        data.parent / "cache-verification.json",
        {
            "vectors": count,
            "counts": {str(k): v for k, v in counts.items()},
            "retrieval_checks": checks,
            "new_embedding_requests": 0,
            "path": str(data / "qdrant"),
        },
    )


def clone(group, expected):
    target = BASE / group
    target.mkdir()
    data = RAM / group
    data.mkdir(parents=True, mode=0o700)
    (target / "data").symlink_to(data, target_is_directory=True)
    with (
        sqlite3.connect(f"file:{SOURCE}/sentrix.db?mode=ro", uri=True) as original,
        sqlite3.connect(data / "sentrix.db") as db,
    ):
        original.backup(db)
        assert fingerprints(db) == expected
        assert not db.execute(
            "select count(*) from agent_turns where status in ('pending','running')"
        ).fetchone()[0]
        assert db.execute("select count(*) from media_cleanup_jobs").fetchone()[0] == 0
        # Disable the abandoned 11/18 build receipt only in the independent QA copy.
        abandoned = db.execute(
            "select operation_id,scope_id from pipeline_receipts where status='running'"
        ).fetchall()
        assert {row[0] for row in abandoned} <= {"8dc3d339db5e429daf3bb2865e614f39"}
        db.execute("update pipeline_receipts set status='cancelled' where status='running'")
        db.execute(
            "update scope_postprocess set processing_generation=null,"
            "dirty_generation=processed_generation,next_attempt_at=null"
        )
        db.commit()
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
        missing = [
            row[0]
            for row in db.execute("select path from assets where scope_id=?", (SCOPE,))
            if not Path(row[0]).is_file()
        ]
        assert not missing, missing[:3]
    for directory in ("media", "previews", "video-jobs", "geo"):
        if (SOURCE / directory).exists():
            (data / directory).symlink_to(SOURCE / directory, target_is_directory=True)
    save(
        target / "clone-verification.json",
        {
            "memory_fingerprints": expected,
            "all_18_tables_identical": True,
            "source_read_only": str(SOURCE),
            "asset_paths_unchanged_and_readable": True,
            "database": str(data / "sentrix.db"),
            "database_storage": "independent tmpfs; evaluation evidence persists on /home",
            "abandoned_build_disabled_in_copy": abandoned,
        },
    )
    build_cache(data)
    return data


def unit(name, values, port, module, appdir):
    environment = BASE / (name + ".env")
    environment.write_text("".join(f"{k}={json.dumps(str(v))}\n" for k, v in sorted(values.items())))
    environment.chmod(0o600)
    path = Path.home() / ".config/systemd/user" / f"{PREFIX}-{name}.service"
    assert not path.exists(), path
    path.write_text(
        f"[Unit]\nDescription=Frozen Doubao Agent direct Flash {name}\n[Service]\n"
        f"WorkingDirectory={BASE}/release\nEnvironmentFile={environment}\nEnvironmentFile={SECRETS}\n"
        f"ExecStart={PYTHON} -m uvicorn --factory {module}:create_app "
        f"--host 0.0.0.0 --port {port} --app-dir {appdir}\n"
        f"Restart=on-failure\nRestartSec=5\nStandardOutput=append:{BASE}/{name}.log\nStandardError=append:{BASE}/{name}.log\n"
    )
    path.chmod(0o600)


def configure(group, ports, data, cloud, revision):
    product, evaluation, models, tools = ports
    target = BASE / group
    (target / "config").mkdir()
    (target / "results").mkdir()
    (target / "runtime").mkdir()
    for name in ("vllm_targets.json", "qa_task_actions.json", "telemetry.json"):
        source = BASELINE / group / "config" / name
        if source.exists():
            shutil.copy2(source, target / "config" / name)
    save(
        target / "config/judge_providers.json",
        {
            "default_provider_id": "qwen38-flash-direct",
            "providers": {"qwen38-flash-direct": {"url": cloud, "model": "qwen3.8-flash", "api_key": ""}},
        },
    )
    runtime = {
        "sentrix_url": f"http://127.0.0.1:{product}",
        "model_base_url": cloud,
        "endpoint_model": "qwen3.8-flash",
        "embedding": {"mode": "unified_multimodal", "wemm_url": "http://127.0.0.1:8102"},
        "product": [],
        "vllm_manager_url": "",
        "product_build_revision": revision,
        "judge_provider_id": "qwen38-flash-direct",
        "judge_url": cloud,
        "judge_model": "qwen3.8-flash",
        "product_settings": {
            "deployment_profile": "server",
            "agent_wall_time_s": 180,
            "chat_timeout_s": 120,
            "reasoning_max_model_calls": models,
            "reasoning_max_tool_calls": tools,
            "reasoning_max_search_calls": 100,
            "reasoning_max_other_tool_calls": 500,
            "vlm_context_tokens": 16384,
            "vlm_reasoning_context_tokens": 16384,
            "agent_turn_workers": 16,
        },
    }
    save(target / "config/runtime_connection.json", runtime)
    common = {
        "PYTHONPATH": BASE / "transport",
        "FLASH_DIRECT_URL": cloud,
        "FLASH_DIRECT_HTTP_DIR": target / "http/raw",
    }
    product_env = load_env(BASELINE / f"{group}-api.env")
    product_env.pop("SENTRIX_LLM_CONFIG", None)
    product_env.pop("SENTRIX_REASONING_MAX_STEPS", None)
    product_env.update(common)
    product_env.update(
        SENTRIX_DATA_DIR=data,
        SENTRIX_API_PORT=product,
        SENTRIX_LLM_PROFILE="local",
        SENTRIX_VLM_BASE_URL=cloud,
        SENTRIX_VLM_MODEL="qwen3.8-flash",
        SENTRIX_VLM_CONTEXT_TOKENS=16384,
        SENTRIX_VLM_REASONING_CONTEXT_TOKENS=16384,
        SENTRIX_AGENT_TURN_MAX_WORKERS=16,
        SENTRIX_REASONING_MAX_MODEL_CALLS=models,
        SENTRIX_REASONING_MAX_TOOL_CALLS=tools,
        SENTRIX_CHAT_TIMEOUT_S=120,
        SENTRIX_VLM_TIMEOUT_S=180,
        PHOTOBENCH_CONFIG_ROOT=target / "config",
    )
    unit(group + "-api", product_env, product, "sentrix.main", BASE / "release/apps/api/src")
    evaluation_env = load_env(BASELINE / f"{group}-eval.env")
    evaluation_env.update(common)
    evaluation_env.update(
        PHOTOBENCH_CONFIG_ROOT=target / "config",
        PHOTOBENCH_RESULTS_ROOT=target / "results",
        PHOTOBENCH_SENTRIX_API_URL=f"http://127.0.0.1:{product}",
        BENCH_SENTRIX_URL=f"http://127.0.0.1:{product}",
        BENCH_VLLM_BASE_URL=cloud,
        BENCH_BIG_MODEL_BASE_URL=cloud,
        BENCH_BIG_MODEL_ENABLED=0,
        PHOTOBENCH_SENTRIX_REPO_ROOT=BASE / "release",
        PHOTOBENCH_SENTRIX_SERVICE_UNIT=f"{PREFIX}-{group}-api.service",
        PHOTOBENCH_SENTRIX_START_COMMAND=f"systemctl --user start {PREFIX}-{group}-api.service",
        PHOTOBENCH_SENTRIX_LOG_PATH=BASE / (group + "-api.log"),
        PHOTOBENCH_SENTRIX_PID_FILE=target / "runtime/api.json",
        PHOTOBENCH_BUILD_REVISION=revision,
        PHOTOBENCH_PORT=evaluation,
    )
    unit(group + "-eval", evaluation_env, evaluation, "photobench.main", BASE / "release/apps/photobench/src")
    payload = json.loads((BASE / "payload-template.json").read_text())
    payload.update(
        existing_scope_id=SCOPE,
        sentrix_url=f"http://127.0.0.1:{product}",
        model_base_url=cloud,
        qa_concurrency=16,
        qa_concurrency_control_path=str(target / "dispatch-control.json"),
        qa_priority_ids=payload["qa_priority_ids"][:16],
        models=["__current__"],
        judge_provider_id="qwen38-flash-direct",
        judge_url=cloud,
        judge_model="qwen3.8-flash",
    )
    assert payload["defer_scoring"] is True and payload["reasoning"] is True
    save(target / "dispatch-control.json", {"concurrency": 16, "pause_after_completed": 16})
    save(target / "run-payload.json", payload)


def prepare():
    assert not (BASE / "prepare-intent.json").exists(), "Inspect prior stage; never replay preparation"
    manifest = json.loads((BASE / "source-sha256.json").read_text())
    assert manifest and all(sha(BASE / name) == digest for name, digest in manifest.items())
    for ports in GROUPS.values():
        for port in ports[:2]:
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0, port
    assert not RAM.exists()
    assert shutil.disk_usage("/dev/shm").free > 2 * (SOURCE / "sentrix.db").stat().st_size + 8 * 1024**3
    assert shutil.disk_usage(BASE).free > 2 * 1024**3
    before = protected()
    cloud = json.loads(CLOUD_CONFIG.read_text())["url"].rstrip("/")
    assert cloud.startswith("https://")
    revision = json.loads((BASE / "release/build-info.json").read_text())["revision"]
    save(
        BASE / "prepare-intent.json",
        {
            "at": time.time(),
            "revision": revision,
            "protected": before,
            "direct_cloud_url": cloud,
            "source": str(SOURCE),
            "qa_concurrency_each": 16,
            "database_copies_authorized_for_new_isolated_instances": True,
        },
    )
    with sqlite3.connect(f"file:{SOURCE}/sentrix.db?mode=ro", uri=True) as db:
        expected = fingerprints(db)
    save(BASE / "source-memory-fingerprints.json", expected)
    for group, ports in GROUPS.items():
        data = clone(group, expected)
        configure(group, ports, data, cloud, revision)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for group in GROUPS:
        for role in ("api", "eval"):
            subprocess.run(["systemctl", "--user", "start", f"{PREFIX}-{group}-{role}.service"], check=True)
    health = {}
    for group, (product, evaluation, models, tools) in GROUPS.items():
        for _ in range(120):
            try:
                live = api(product, "health/live")
                config = api(evaluation, "config")
                assert live["build_revision"] == revision
                assert config["runtime_config"]["product_build_revision"] == revision
                break
            except (OSError, AssertionError):
                time.sleep(2)
        else:
            raise TimeoutError(f"{group} service startup: inspect its own logs")
        diagnostics = api(product, "diagnostics")
        effective = diagnostics["runtime_configuration"]
        for key, value in {
            "reasoning_max_model_calls": models,
            "reasoning_max_tool_calls": tools,
            "vlm_context_tokens": 16384,
            "vlm_reasoning_context_tokens": 16384,
            "agent_turn_workers": 16,
            "vlm_base_url": cloud,
        }.items():
            assert effective[key] == value, (group, key, effective.get(key), value)
        with sqlite3.connect(f"file:{RAM}/{group}/sentrix.db?mode=ro", uri=True) as db:
            assert fingerprints(db) == expected
        health[group] = {"live": live, "settings": effective, "memory_18_tables_preserved": True}
    verify_protected(before)
    save(
        BASE / "prepared.json",
        {
            "at": time.time(),
            "revision": revision,
            "health": health,
            "protected_processes_unchanged": before,
            "new_qa_started": False,
            "agent_source_unchanged": True,
            "direct_cloud": True,
        },
    )


if __name__ == "__main__":
    try:
        prepare()
    except BaseException as error:
        save(
            BASE / "prepare-terminal.json",
            {"status": "failed", "at": time.time(), "error": str(error), "error_type": type(error).__name__},
        )
        raise
    save(BASE / "prepare-terminal.json", {"status": "completed", "at": time.time()})
