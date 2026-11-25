"""Move the immutable SQLite snapshot across disks, keeping its public path and SHA."""

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-agent-candidates-20261126")
SOURCE = BASE.with_name("qwen38-flash-20261124") / (
    "snapshots/atm-flash-no-thinking-final-20261126-v1/sentrix.db"
)
TARGET = Path("/var/tmp/realmagic-flash-snapshots/atm-flash-no-thinking-final-20261126-v1.db")


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run():
    receipt = BASE / "snapshot-relocation.json"
    assert not receipt.exists() and not SOURCE.is_symlink()
    assert SOURCE.stat().st_nlink == 1 and SOURCE.stat().st_mode & 0o222 == 0
    assert not TARGET.exists()
    assert shutil.disk_usage(TARGET.parent.parent).free > SOURCE.stat().st_size + 2 * 1024**3
    TARGET.parent.mkdir(mode=0o700, exist_ok=True)
    original = sha(SOURCE)
    temporary = TARGET.with_suffix(".db.copying")
    assert not temporary.exists()
    shutil.copy2(SOURCE, temporary)
    assert sha(temporary) == original
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    temporary.replace(TARGET)
    link = SOURCE.with_suffix(".db.relocated")
    assert not link.exists()
    link.symlink_to(TARGET)
    link.replace(SOURCE)
    assert SOURCE.resolve() == TARGET and sha(SOURCE) == original
    data = {
        "source": str(SOURCE),
        "target": str(TARGET),
        "sha256": original,
        "bytes": TARGET.stat().st_size,
        "at": time.time(),
        "source_path_preserved": True,
        "snapshot_bytes_unchanged": True,
        "active_databases_moved": 0,
        "home_free_bytes": shutil.disk_usage(BASE).free,
        "root_free_bytes": shutil.disk_usage(TARGET).free,
    }
    receipt.write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps(data), flush=True)


if __name__ == "__main__":
    run()
