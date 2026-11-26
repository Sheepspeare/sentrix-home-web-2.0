"""Frozen Doubao Agent with the independently frozen Flash memory, direct cloud I/O."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

BASE = Path(
    "/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-doubao-code-flash-memory-direct-20261126"
)
RAM = Path("/dev/shm/sentrix-doubao-code-flash-memory-direct-20261126")
SNAPSHOT = BASE.with_name("qwen38-flash-20261124") / "snapshots/atm-flash-no-thinking-final-20261126-v1"
ORIGINAL_DATA = "/var/tmp/sentrix-qwen38-flash-20261124/product-data"
SCOPE = "PhotoBench-20261124-221506-2026.11.17-atm-sources-addresses-qwen3.8-flash"
GROUPS = {"limited": (8305, 8885, 20, 30), "large": (8306, 8886, 100, 600)}
DB_SHA = "a82ad2a317eae4e2236bcf17f68c53a4485d5c425024ef81801616e1b314de82"


def normalized_fingerprints(op, db, data):
    """Compare all memory values; normalize only the explicitly mapped asset path."""
    result = op.raw_fingerprints(db)
    columns = [row[1] for row in db.execute('pragma table_info("assets")')]
    path_index = columns.index("path")
    import hashlib

    digest = hashlib.sha256()
    count = 0
    keys = [row[1] for row in sorted(db.execute('pragma table_info("assets")'), key=lambda r: r[5]) if row[5]]
    order = ",".join('"' + key + '"' for key in keys) or "rowid"
    for row in db.execute(f"SELECT * FROM assets ORDER BY {order}"):
        values = [value.hex() if isinstance(value, bytes) else value for value in row]
        path = values[path_index]
        assert path.startswith(str(data) + "/"), path
        values[path_index] = ORIGINAL_DATA + path[len(str(data)) :]
        digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
        digest.update(b"\n")
        count += 1
    result["assets"] = {"count": count, "sha256": digest.hexdigest()}
    return result


def clone(op, group, expected):
    target = BASE / group
    target.mkdir()
    data = RAM / group
    data.mkdir(parents=True, mode=0o700)
    (target / "data").symlink_to(data, target_is_directory=True)
    for folder in ("media", "previews", "video-jobs", "geo"):
        if (SNAPSHOT / folder).exists():
            (data / folder).symlink_to(SNAPSHOT / folder, target_is_directory=True)
    with (
        sqlite3.connect(f"file:{SNAPSHOT}/sentrix.db?mode=ro", uri=True) as source,
        sqlite3.connect(data / "sentrix.db") as db,
    ):
        source.backup(db)
        assert op.fingerprints(db) == expected
        paths = [row[0] for row in db.execute("select path from assets")]
        assert len(paths) == 13356
        assert all(path.startswith(ORIGINAL_DATA + "/") for path in paths)
        db.execute("update assets set path=? || substr(path,?)", (str(data), len(ORIGINAL_DATA) + 1))
        db.commit()
        assert all(Path(row[0]).is_file() for row in db.execute("select path from assets"))
        for table in ("agent_turns", "import_jobs", "video_jobs", "pipeline_receipts"):
            statuses = dict(db.execute(f"select status,count(*) from {table} group by status"))
            assert not set(statuses) & {
                "accepted",
                "queued",
                "running",
                "processing",
                "pending",
                "RUNNING",
                "QUEUED",
                "RETRYING",
                "PENDING",
                "RETRY_WAIT",
            }, (table, statuses)
        assert db.execute("select count(*) from media_cleanup_jobs").fetchone()[0] == 0
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
        assert normalized_fingerprints(op, db, data) == expected
        assert (
            db.execute("select count(*) from observations where scope_id=?", (SCOPE,)).fetchone()[0] == 13356
        )
    op.save(
        target / "clone-verification.json",
        {
            "snapshot": str(SNAPSHOT),
            "snapshot_db_sha256": DB_SHA,
            "scope_id": SCOPE,
            "mapped_asset_paths": 13356,
            "memory_fingerprints": expected,
            "all_18_tables_identical_after_path_normalization": True,
            "database": str(data / "sentrix.db"),
            "media": "shared read-only frozen snapshot",
            "new_build_requests": 0,
            "new_embedding_requests": 0,
        },
    )
    op.build_cache(data)
    return data


def main():
    spec = importlib.util.spec_from_file_location("direct_prepare", Path(__file__).with_name("prepare.py"))
    op = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(op)
    op.BASE, op.RAM, op.SOURCE, op.SCOPE, op.GROUPS = BASE, RAM, SNAPSHOT, SCOPE, GROUPS
    op.PREFIX = "sentrix-flash-own-memory-direct"
    original_protected = op.protected
    op.raw_fingerprints = op.fingerprints

    def fingerprints(db):
        row = db.execute("select path from assets limit 1").fetchone()
        if row and row[0].startswith(str(RAM) + "/"):
            group = Path(row[0]).relative_to(RAM).parts[0]
            assert group in GROUPS
            return normalized_fingerprints(op, db, RAM / group)
        return op.raw_fingerprints(db)

    op.fingerprints = fingerprints

    def protected():
        result = original_protected()
        units = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=running",
                "--no-legend",
            ],
            text=True,
        )
        for line in units.splitlines():
            name = line.split()[0]
            if name.startswith("sentrix-flash-") and not name.startswith(op.PREFIX + "-"):
                pid = subprocess.check_output(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        name,
                        "-p",
                        "MainPID",
                        "--value",
                    ],
                    text=True,
                ).strip()
                result[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
        return result

    op.protected = protected
    op.clone = lambda group, expected: clone(op, group, expected)
    try:
        assert not (BASE / "prepare-intent.json").exists(), "Inspect prior preparation; never replay"
        assert shutil.disk_usage(RAM.parent).free > 2 * (SNAPSHOT / "sentrix.db").stat().st_size + 8 * 1024**3
        assert op.sha(SNAPSHOT / "sentrix.db") == DB_SHA
        with sqlite3.connect(f"file:{SNAPSHOT}/sentrix.db?mode=ro", uri=True) as db:
            expected = json.loads((SNAPSHOT / "memory-fingerprints.json").read_text())
            assert op.fingerprints(db) == expected
        op.prepare()
        receipt = json.loads((BASE / "prepared.json").read_text())
        receipt.update(
            memory_source="Flash no-thinking frozen album",
            scope_id=SCOPE,
            source_snapshot_db_sha256=DB_SHA,
            new_embedding_requests=0,
        )
        op.save(BASE / "prepared.json", receipt)
    except BaseException as error:
        op.save(
            BASE / "prepare-terminal.json",
            {
                "status": "failed",
                "at": time.time(),
                "error": str(error),
                "error_type": type(error).__name__,
            },
        )
        raise
    op.save(BASE / "prepare-terminal.json", {"status": "completed", "at": time.time()})


if __name__ == "__main__":
    main()
