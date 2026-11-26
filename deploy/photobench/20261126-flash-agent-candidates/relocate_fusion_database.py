"""Relocate only the paused fusion product DB, retaining an exact durable gzip archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-condition-fusion-20261126")
DESTINATION = Path("/dev/shm/sentrix-condition-fusion-20261126/product-db")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_exact(source, destination):
    """Keep the original until compressed bytes round-trip with the exact SHA and size."""
    assert not destination.exists()
    part = destination.with_suffix(destination.suffix + ".part")
    with source.open("rb") as incoming, part.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024**2)
        raw.flush()
        os.fsync(raw.fileno())
    checksum = hashlib.sha256()
    count = 0
    with gzip.open(part, "rb") as stream:
        while block := stream.read(1024**2):
            checksum.update(block)
            count += len(block)
    assert count == source.stat().st_size and checksum.hexdigest() == digest(source)
    part.replace(destination)
    return {
        "source_sha256": checksum.hexdigest(),
        "source_size": count,
        "gzip_sha256": digest(destination),
        "gzip_size": destination.stat().st_size,
    }


def probe():
    database = BASE / "large/data/sentrix.db"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        statuses = dict(db.execute("select status,count(*) from agent_turns group by status"))
        recent = db.execute(
            "select id,status,updated_at from agent_turns order by created_at desc limit 16"
        ).fetchall()
    return {
        "turn_statuses": statuses,
        "recent_turns": recent,
        "home_free_bytes": shutil.disk_usage(BASE).free,
        "ram_free_bytes": shutil.disk_usage(Path("/dev/shm")).free,
    }


def run(run_id, helpers):
    sys.path.insert(0, str(helpers))
    import revise_fusion

    op = revise_fusion.revise_limited
    h = op.h
    current = h.api(8883, f"runs/{run_id}?include_history=false")
    progress = current["phases"]["qa_eval"]["progress"]
    assert progress["agent_completed"] == 16 and progress["dispatch_paused"]
    assert current.get("fatal_error") is None
    database = BASE / "large/data/sentrix.db"
    assert database.is_file() and not database.is_symlink()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        assert (
            db.execute("select count(*) from agent_turns where status in ('pending','running')").fetchone()[0]
            == 0
        )
    assert shutil.disk_usage(Path("/dev/shm")).free > database.stat().st_size + 2 * 1024**3
    output = BASE / "database-storage-recovery"
    output.mkdir()
    protected = revise_fusion.protected_processes()
    h.save(
        output / "intent.json",
        {
            "run_id": run_id,
            "database": str(database),
            "destination": str(DESTINATION),
            "protected": protected,
            "at": time.time(),
            "scope": (
                "only paused fusion product; evaluation remains paused; preserve original bytes in gzip"
            ),
        },
    )
    unit = "sentrix-flash-condition-fusion-large-api.service"
    subprocess.run(["systemctl", "--user", "stop", unit], check=True)
    with sqlite3.connect(database) as db:
        assert db.execute("pragma wal_checkpoint(truncate)").fetchone()[0] == 0
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
    source = json.loads((h.SNAPSHOT / "memory-fingerprints.json").read_text())
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        assert h.memory_fingerprints(db, source, database.parent) == source
    checksum = digest(database)
    DESTINATION.mkdir(parents=True)
    ram_db = DESTINATION / "sentrix.db"
    shutil.copy2(database, ram_db)
    assert digest(ram_db) == checksum
    archive = output / "before-relocation.db.gz"
    ram_archive = DESTINATION / "before-relocation.db.gz"
    receipt = archive_exact(database, ram_archive)
    assert shutil.disk_usage(output).free > ram_archive.stat().st_size + 512 * 1024**2
    archive_part = archive.with_suffix(archive.suffix + ".part")
    shutil.copy2(ram_archive, archive_part)
    assert digest(archive_part) == receipt["gzip_sha256"]
    archive_part.replace(archive)
    h.save(output / "archive-verification.json", receipt)
    ram_archive.unlink()
    temporary = database.with_suffix(".relocating")
    temporary.symlink_to(ram_db)
    temporary.replace(database)
    h.save(
        output / "relocated.json",
        {
            "database": str(database),
            "target": str(ram_db),
            "sha256": checksum,
            "restore": f"gzip -dc {archive} > restored.db; verify SHA before replacing link",
            "at": time.time(),
        },
    )
    subprocess.run(["systemctl", "--user", "start", unit], check=True)

    def healthy():
        try:
            return h.api(8303, "health/live")["build_revision"] == current["build_revision"]
        except OSError:
            return False

    op.wait_for(healthy)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        assert h.memory_fingerprints(db, source, database.parent) == source
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in protected.items())
    h.save(
        output / "verification.json",
        {
            **receipt,
            "at": time.time(),
            "memory_18_tables_unchanged": True,
            "protected_processes_unchanged": protected,
            "evaluation_process_restarted": False,
            "run_id": run_id,
            **probe(),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--helpers", type=Path)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe()))
    else:
        assert args.run_id and args.helpers
        run(args.run_id, args.helpers)
