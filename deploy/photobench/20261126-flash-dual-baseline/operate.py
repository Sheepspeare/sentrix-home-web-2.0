"""Prepare two isolated QA instances from the accepted immutable album."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OLD = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/qwen38-flash-20261124")
BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-dual-baseline-20261126")
SNAPSHOT = OLD / "snapshots/atm-flash-no-thinking-final-20261126-v1"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"
ORIGINAL_DATA = "/var/tmp/sentrix-qwen38-flash-20261124/product-data"
SCOPE = "PhotoBench-20261124-221506-2026.11.17-atm-sources-addresses-qwen3.8-flash"
GROUPS = {"limited": (8296, 8876, 20, 30), "large": (8297, 8877, 100, 600)}
GATEWAY = "http://127.0.0.1:18148/v1"
ALBUM = "2026.11.17-atm-sources-addresses"


def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def api(port, path, payload=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def env(path, values):
    path.write_text("".join(f"{k}={json.dumps(str(v))}\n" for k, v in sorted(values.items())))
    path.chmod(0o600)


def load_env(path):
    return {
        k: json.loads(v)
        for k, v in (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    }


def unit(name, values, port, module, appdir, secrets=False):
    file = BASE / (name + ".env")
    env(file, values)
    text = (
        f"[Unit]\nDescription=Flash dual baseline {name}\n[Service]\n"
        f"WorkingDirectory={BASE}/release\nEnvironmentFile={file}\n"
    )
    if secrets:
        text += f"EnvironmentFile={OLD}/secrets.env\n"
    text += (
        f"ExecStart={PYTHON} -m uvicorn --factory {module}:create_app --host 0.0.0.0 "
        f"--port {port} --app-dir {appdir}\nRestart=on-failure\nRestartSec=5\n"
        f"StandardOutput=append:{BASE}/{name}.log\nStandardError=append:{BASE}/{name}.log\n"
    )
    target = Path.home() / ".config/systemd/user" / f"sentrix-flash-dual-{name}.service"
    assert not target.exists(), target
    target.write_text(text)
    target.chmod(0o600)


def memory_fingerprints(db, tables, data):
    result = {}
    for table in tables:
        columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = ",".join('"' + key + '"' for key in keys) or "rowid"
        path_column = [row[1] for row in columns].index("path") if table == "assets" else None
        digest = hashlib.sha256()
        count = 0
        for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            values = [value.hex() if isinstance(value, bytes) else value for value in row]
            if path_column is not None:
                mapped = values[path_column]
                assert mapped.startswith(str(data) + "/")
                values[path_column] = ORIGINAL_DATA + mapped[len(str(data)) :]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
            count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def prepare():
    assert not (BASE / "prepare-intent.json").exists(), "Inspect existing outcome"
    assert shutil.disk_usage(BASE).free > 8 * 1024**3
    revision = json.loads((BASE / "release/build-info.json").read_text())["revision"]
    manifest = json.loads((BASE / "source-sha256.json").read_text())
    assert all(sha(BASE / "release" / name) == digest for name, digest in manifest.items())
    protected = {
        str(pid): Path(f"/proc/{pid}/stat").read_text().split()[21] for pid in (4191434, 2427495, 2427487)
    }
    save(BASE / "prepare-intent.json", {"at": time.time(), "revision": revision, "snapshot": str(SNAPSHOT)})
    gc = json.loads((OLD / "gateway-config.json").read_text())
    gc["max_concurrency"] = 8
    save(BASE / "gateway-config.json", gc)
    unit(
        "gateway",
        {
            "FLASH_GATEWAY_CONFIG": BASE / "gateway-config.json",
            "FLASH_REQUEST_LOG": BASE / "model-requests.jsonl",
        },
        18148,
        "gateway",
        BASE / "scripts",
        True,
    )
    source_profile = json.loads((OLD / "model-profile.json").read_text())
    for role in source_profile["roles"].values():
        role["base_url"] = GATEWAY
    save(BASE / "model-profile.json", source_profile)
    original_config = json.loads((OLD / "config/runtime_connection.json").read_text())
    source_fingerprint = json.loads((SNAPSHOT / "memory-fingerprints.json").read_text())
    for group, (product, evaluation, models, tools) in GROUPS.items():
        target = BASE / group
        target.mkdir()
        data = target / "data"
        data.mkdir()
        shutil.copy2(SNAPSHOT / "sentrix.db", data / "sentrix.db")
        (data / "sentrix.db").chmod(0o600)
        for directory in ["media", "previews", "video-jobs", "geo"]:
            if (SNAPSHOT / directory).exists():
                (data / directory).symlink_to(SNAPSHOT / directory, target_is_directory=True)
        db = sqlite3.connect(data / "sentrix.db")
        db.row_factory = sqlite3.Row
        before = dict(db.execute("select id,path from assets").fetchall())
        assert len(before) == 13356
        assert all(p.startswith(ORIGINAL_DATA + "/") for p in before.values())
        db.execute("update assets set path=? || substr(path,?)", (str(data), len(ORIGINAL_DATA) + 1))
        db.commit()
        assert all(Path(r[0]).is_file() for r in db.execute("select path from assets"))
        for table in ["agent_turns", "import_jobs", "video_jobs", "pipeline_receipts"]:
            statuses = dict(db.execute(f"select status,count(*) from {table} group by status"))
            assert not set(statuses) & {
                "accepted",
                "queued",
                "running",
                "processing",
                "RUNNING",
                "QUEUED",
                "RETRYING",
                "PENDING",
                "RETRY_WAIT",
            }, (table, statuses)
        assert db.execute("select count(*) from media_cleanup_jobs").fetchone()[0] == 0
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
        actual_fingerprint = memory_fingerprints(db, source_fingerprint, data)
        assert actual_fingerprint == source_fingerprint, "Clone changed memory outside asset path mapping"
        counts = {
            t: db.execute(f"select count(*) from {t}").fetchone()[0]
            for t in [
                "assets",
                "observations",
                "events",
                "memory_vectors",
                "text_vectors",
                "memory_graph_nodes",
                "memory_graph_edges",
            ]
        }
        assert list(counts.values()) == [13356, 13356, 4324, 6989, 31036, 71122, 292835], counts
        save(
            target / "clone-verification.json",
            {
                "counts": counts,
                "snapshot_db_sha256": sha(SNAPSHOT / "sentrix.db"),
                "mapped_paths": len(before),
                "media": "read-only snapshot shared by symlink",
                "memory_fingerprints": actual_fingerprint,
                "memory_matches_snapshot_after_path_normalization": True,
            },
        )
        db.close()
        cache = Path("/dev/shm") / f"sentrix-flash-dual-20261126-{group}"
        cache.mkdir(mode=0o700)
        (data / "qdrant").symlink_to(cache, target_is_directory=True)
        build_cache(data)
        config = target / "config"
        shutil.copytree(OLD / "config", config)
        connection = json.loads(json.dumps(original_config))
        connection.update(
            sentrix_url=f"http://127.0.0.1:{product}", model_base_url=GATEWAY, product_build_revision=revision
        )
        settings = connection["product_settings"]
        settings.update(
            reasoning_max_steps=100,
            reasoning_max_model_calls=models,
            reasoning_max_tool_calls=tools,
            reasoning_max_search_calls=100,
            reasoning_max_other_tool_calls=500,
            agent_turn_workers=4,
            vlm_reasoning_context_tokens=131072,
        )
        save(config / "runtime_connection.json", connection)
        for folder in ["results", "runtime"]:
            (target / folder).mkdir()
        product_env = load_env(OLD / "product.env")
        product_env = {
            k: v
            for k, v in product_env.items()
            if k.startswith(
                (
                    "SENTRIX_",
                    "FACE_",
                    "RETINAFACE_",
                    "HF_",
                    "TRANSFORMERS_",
                    "OMP_",
                    "MKL_",
                    "OPENBLAS_",
                    "NUMEXPR_",
                )
            )
        }
        product_env.update(
            SENTRIX_DATA_DIR=data,
            SENTRIX_API_PORT=product,
            SENTRIX_LLM_CONFIG=BASE / "model-profile.json",
            SENTRIX_VLM_BASE_URL=GATEWAY,
            SENTRIX_AGENT_TURN_MAX_WORKERS=4,
            SENTRIX_REASONING_MAX_STEPS=100,
            SENTRIX_VLM_REASONING_CONTEXT_TOKENS=131072,
            SENTRIX_REASONING_MAX_MODEL_CALLS=models,
            SENTRIX_REASONING_MAX_TOOL_CALLS=tools,
            SENTRIX_REASONING_MAX_SEARCH_CALLS=100,
            SENTRIX_REASONING_MAX_OTHER_TOOL_CALLS=500,
        )
        unit(group + "-api", product_env, product, "sentrix.main", BASE / "release/apps/api/src", True)
        evaluation_env = load_env(OLD / "evaluation.env")
        evaluation_env = {k: v for k, v in evaluation_env.items() if not k.startswith("SENTRIX_")}
        evaluation_env.update(
            PHOTOBENCH_CONFIG_ROOT=config,
            PHOTOBENCH_RESULTS_ROOT=target / "results",
            PHOTOBENCH_SENTRIX_API_URL=f"http://127.0.0.1:{product}",
            BENCH_SENTRIX_URL=f"http://127.0.0.1:{product}",
            BENCH_VLLM_BASE_URL=GATEWAY,
            PHOTOBENCH_SENTRIX_REPO_ROOT=BASE / "release",
            PHOTOBENCH_SENTRIX_SERVICE_UNIT=f"sentrix-flash-dual-{group}-api.service",
            PHOTOBENCH_SENTRIX_START_COMMAND=f"systemctl --user start sentrix-flash-dual-{group}-api.service",
            PHOTOBENCH_SENTRIX_LOG_PATH=BASE / (group + "-api.log"),
            PHOTOBENCH_SENTRIX_PID_FILE=target / "runtime/api.json",
            PHOTOBENCH_BUILD_REVISION=revision,
            PHOTOBENCH_PORT=evaluation,
        )
        unit(
            group + "-eval",
            evaluation_env,
            evaluation,
            "photobench.main",
            BASE / "release/apps/photobench/src",
        )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", "sentrix-flash-dual-gateway.service"], check=True)
    for group in GROUPS:
        subprocess.run(
            ["systemctl", "--user", "start", f"sentrix-flash-dual-{group}-api.service"], check=True
        )
        subprocess.run(
            ["systemctl", "--user", "start", f"sentrix-flash-dual-{group}-eval.service"], check=True
        )
    health = {}
    for group, (product, evaluation, models, tools) in GROUPS.items():
        last_error = ""
        for _attempt in range(90):
            try:
                live = api(product, "health/live")
                cfg = api(evaluation, "config")
                assert live["build_revision"] == revision
                assert cfg["runtime_config"]["product_build_revision"] == revision
                break
            except Exception as error:
                last_error = str(error)
                time.sleep(2)
        else:
            raise RuntimeError(f"{group} startup failed: {last_error}")
        actual = api(product, "diagnostics")
        settings = actual["runtime_configuration"]
        assert settings["reasoning_max_steps"] == 100
        assert settings["reasoning_max_model_calls"] == models
        assert settings["reasoning_max_tool_calls"] == tools
        assert settings["agent_turn_workers"] == 4
        assert settings["vlm_reasoning_context_tokens"] == 131072
        assert actual["database"] == "ok" and actual["embeddings"] == "ok", actual
        with sqlite3.connect(BASE / group / "data/sentrix.db") as db:
            db.row_factory = sqlite3.Row
            fingerprints = memory_fingerprints(db, source_fingerprint, BASE / group / "data")
            assert fingerprints == source_fingerprint, "Startup changed frozen memory"
        health[group] = {"live": live, "diagnostics": actual, "post_start_memory_matches_snapshot": True}
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == start for pid, start in protected.items())
    save(
        BASE / "prepared.json",
        {
            "at": time.time(),
            "revision": revision,
            "protected_processes_unchanged": protected,
            "health": health,
        },
    )


def build_cache(data):
    sys.path.insert(0, str(BASE / "release/apps/api/src"))
    from sentrix.domain.retrieval.ports import VectorRecord
    from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

    db = sqlite3.connect(data / "sentrix.db")
    db.row_factory = sqlite3.Row
    index = QdrantVectorIndex(data / "qdrant")
    records = []
    count = 0
    for row in db.execute("select * from memory_vectors"):
        records.append(
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
        if len(records) == 128:
            count += index.upsert(records)
            records = []
    count += index.upsert(records)
    assert count == 6989
    counts = {
        (row[0], row[1]): index.count(space=row[0], model_name=row[1])
        for row in db.execute("select distinct space,model_name from memory_vectors")
    }
    assert sum(counts.values()) == count, counts
    retrieval = []
    for row in db.execute("select * from memory_vectors order by id limit 3"):
        hits = index.search(
            space=row["space"],
            model_name=row["model_name"],
            vector=json.loads(row["vector_json"]),
            scope_id=row["scope_id"],
            limit=10,
        )
        assert any(hit.source_id == row["source_id"] for hit in hits)
        retrieval.append({"source_id": row["source_id"], "hits": [hit.source_id for hit in hits]})
    index.close()
    db.close()
    save(
        data.parent / "cache-verification.json",
        {
            "retrieval_checks": retrieval,
            "vectors": count,
            "verified_index_counts": {str(k): v for k, v in counts.items()},
            "new_embedding_requests": 0,
            "path": str((data / "qdrant").resolve()),
            "cache_lifetime": "tmpfs; rebuild from saved SQLite vectors after reboot",
        },
    )


if __name__ == "__main__":
    try:
        prepare()
    except BaseException as error:
        save(
            BASE / "prepare-terminal.json",
            {"status": "failed", "error": str(error), "error_type": type(error).__name__, "at": time.time()},
        )
        raise
    save(BASE / "prepare-terminal.json", {"status": "completed", "at": time.time()})
