"""Losslessly archive the four retired Flash build recovery databases."""

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

RECOVERY = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/qwen38-flash-20261124/repair-ee6594ae")
SOURCES = [
    RECOVERY / "before-recovery/sentrix.db",
    RECOVERY / "content/before-content.db",
    RECOVERY / "content/legacy-scene-summaries/before-legacy.db",
    RECOVERY / "content/visual-review-v2/apply/before-apply.db",
]


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def digest(stream):
    sha = hashlib.sha256()
    count = 0
    while block := stream.read(1024 * 1024):
        sha.update(block)
        count += len(block)
    return count, sha.hexdigest()


def archive(source):
    compressed = source.with_name(source.name + ".gz")
    receipt = source.with_name(source.name + ".archive.json")
    temporary = compressed.with_name(compressed.name + ".part")
    assert source.is_file() and not source.is_symlink(), str(source)
    assert not compressed.exists() and not receipt.exists() and not temporary.exists()
    stat = source.stat()
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    expected = hashlib.sha256()
    with source.open("rb") as original, temporary.open("xb") as destination:
        with gzip.GzipFile(filename="", fileobj=destination, mode="wb", compresslevel=1, mtime=0) as output:
            while block := original.read(1024 * 1024):
                expected.update(block)
                output.write(block)
        destination.flush()
        os.fsync(destination.fileno())
    with gzip.open(temporary, "rb") as decoded:
        size, sha = digest(decoded)
    current = source.stat()
    assert signature == (current.st_ino, current.st_size, current.st_mtime_ns), "source changed"
    assert (size, sha) == (stat.st_size, expected.hexdigest()), "archive mismatch"
    temporary.chmod(0o444)
    temporary.replace(compressed)
    with compressed.open("rb") as stream:
        archive_size, archive_sha = digest(stream)
    result = {
        "source": str(source),
        "archive": str(compressed),
        "source_bytes": size,
        "source_sha256": sha,
        "archive_bytes": archive_size,
        "archive_sha256": archive_sha,
        "source_mode": stat.st_mode & 0o777,
        "source_mtime_ns": stat.st_mtime_ns,
        "decoded_bytes_verified": True,
        "at": time.time(),
        "restore": "Decode archive with gzip to a new file, verify source_sha256, then restore source path.",
    }
    save(receipt, result)
    # Only the retired uncompressed backup is released; its full bytes remain in gzip.
    source.unlink()
    return result


def main(output):
    output.mkdir()
    opened = subprocess.run(["lsof", "-t", "--", *map(str, SOURCES)], capture_output=True, text=True)
    assert opened.returncode == 1 and not opened.stdout.strip(), "backup is open; inspect before archiving"
    for source in SOURCES:
        assert not Path(str(source) + "-wal").exists() or Path(str(source) + "-wal").stat().st_size == 0
    save(output / "intent.json", {"sources": list(map(str, SOURCES)), "at": time.time()})
    results = []
    try:
        for source in SOURCES:
            result = archive(source)
            results.append(result)
            save(output / "progress.json", {"completed": results})
        result = {"status": "completed", "archives": results, "at": time.time()}
        save(output / "terminal.json", result)
        print(json.dumps(result), flush=True)
        return 0
    except Exception as error:
        save(output / "terminal.json", {"status": "failed", "error": str(error), "completed": results})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output))
