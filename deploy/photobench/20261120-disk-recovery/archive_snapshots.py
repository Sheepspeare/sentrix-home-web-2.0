"""Compress exactly four inactive legacy snapshot files, verifying decompressed SHA first."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path("/home/realmagic/xy/experiments/atm1013-20261118")
HERE = Path(__file__).resolve().parent
ROOT = Path("/home/realmagic/xy/atm-bench-20261110/results")
RUN = "20261118-111519-2026.11.17-atm-sources-addresses-current-resume-90f21f"
FILES = {
    "39b453c22a8e483c9dea6c16abb87d4e/results.jsonl": 2168355387,
    "39b453c22a8e483c9dea6c16abb87d4e/run.json": 2170811296,
    "0153862e46f84a6c88ee5a47757604c8/results.jsonl": 2168355387,
    "0153862e46f84a6c88ee5a47757604c8/run.json": 2170851947,
}
SOURCE = BASE / "chat-trace-8872-7b34a75b/apps/photobench/src"
ENCODING_SHA = "767ed85d810f4790444df684939d3709ec0945723acb0b21ed8c9e355de3ec2b"
CHUNK = 1024 * 1024


def sha(path, compressed=False):
    digest = hashlib.sha256()
    opener = gzip.open if compressed else open
    with opener(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def save(name, data):
    path = HERE / name
    temp = path.with_suffix(".partial")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


def stamp(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def compress_one(path, minimum_free=256 * 1024**2):
    before = stamp(path)
    target = path.with_name(path.name + ".gz")
    partial = path.with_name(path.name + ".gz.partial")
    if target.exists() or partial.exists():
        raise RuntimeError(f"Compression destination already exists: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source, partial.open("xb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=1, mtime=0) as zipped:
                for block in iter(lambda: source.read(CHUNK), b""):
                    if shutil.disk_usage(path.parent).free < minimum_free:
                        raise RuntimeError("Space below compression reserve; original retained")
                    digest.update(block)
                    zipped.write(block)
            output.flush()
            os.fsync(output.fileno())
        expected = digest.hexdigest()
        if sha(partial, compressed=True) != expected or stamp(path) != before:
            raise RuntimeError("Decompressed SHA or original file identity changed")
        if partial.stat().st_size >= before[2]:
            raise RuntimeError("Compression did not save disk space")
        os.chmod(partial, path.stat().st_mode & 0o777)
        partial.rename(target)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
            path.unlink()
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "path": str(path),
            "bytes_before": before[2],
            "bytes_after": target.stat().st_size,
            "uncompressed_sha256": expected,
            "roundtrip_verified": True,
        }
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def preflight():
    run = ROOT / RUN
    for port in (8872, 8874):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/active-work", timeout=10) as response:
            assert all(row["id"] != RUN for row in json.load(response)["work"])
    mappings = json.loads((BASE / "active-runs.json").read_text())
    assert all(row.get("run_id") != RUN for row in mappings.values() if isinstance(row, dict))
    assert sha(SOURCE / "photobench/infrastructure/results/encoding.py") == ENCODING_SHA
    other = BASE / "wait-chat-950b2be/evaluation/apps/photobench/src"
    assert sha(other / "photobench/infrastructure/results/encoding.py") == ENCODING_SHA
    sys.path.insert(0, str(SOURCE))
    from photobench.infrastructure.results.encoding import snapshot_file

    for relative, size in FILES.items():
        path = run / ".snapshots" / relative
        assert path.is_file() and not path.is_symlink() and path.stat().st_size == size
        assert not path.with_name(path.name + ".gz").exists()
        metadata = json.loads((path.parent / "metadata.json").read_text())
        assert metadata["entry"]["run_id"] == RUN
        assert metadata["entry"]["status"] not in {"running", "pending", "cancelling"}
        assert snapshot_file(path.parent, path.name) == path
    protected = [run / name for name in ("snapshot-commit.json", "run.json", "results.jsonl")]
    protected += sorted((run / ".snapshots").glob("*/metadata.json"))
    return {str(path): {"sha256": sha(path), "stat": stamp(path)} for path in protected}


def work():
    os.nice(15)
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    progress = {
        "status": "running",
        "worker_pid": os.getpid(),
        "run_id": RUN,
        "started_at": time.time(),
        "files": [],
        "disk_free_before": shutil.disk_usage(ROOT).free,
    }
    save("progress.json", progress)
    try:
        protected = preflight()
        save("preservation-before.json", protected)
        from photobench.infrastructure.results.encoding import snapshot_file

        for relative in FILES:
            progress["current_file"] = relative
            save("progress.json", progress)
            path = ROOT / RUN / ".snapshots" / relative
            receipt = compress_one(path)
            assert snapshot_file(path.parent, path.name) == path.with_name(path.name + ".gz")
            progress["files"].append(receipt)
            progress["disk_free"] = shutil.disk_usage(ROOT).free
            save("progress.json", progress)
        assert all(
            sha(Path(path)) == value["sha256"] and stamp(Path(path)) == value["stat"]
            for path, value in protected.items()
        )
        progress.update(
            status="completed",
            protected_files_unchanged=True,
            finished_at=time.time(),
            memory_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        save("acceptance.json", progress)
    except BaseException as error:
        progress.update(status="failed", error=repr(error), finished_at=time.time())
        raise
    finally:
        save("progress.json", progress)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("launch", "work", "probe"))
    parser.add_argument("--wait-id")
    args = parser.parse_args()
    if args.operation == "launch":
        assert args.wait_id
        with (HERE / "intent.json").open("x") as stream:
            json.dump(
                {"wait_id": args.wait_id, "run_id": RUN, "files": FILES, "created_at": time.time()},
                stream,
                indent=2,
            )
        assert not (HERE / "progress.json").exists() and not (HERE / "response.json").exists()
        with (HERE / "worker.log").open("xb") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "work"],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        receipt = {
            "worker_pid": process.pid,
            "wait_id": args.wait_id,
            "run_id": RUN,
            "started_at": time.time(),
        }
        save("response.json", receipt)
        print(json.dumps(receipt), flush=True)
    elif args.operation == "work":
        work()
    else:
        receipt = json.loads((HERE / "response.json").read_text())
        progress = (
            json.loads((HERE / "progress.json").read_text()) if (HERE / "progress.json").exists() else {}
        )
        pid = receipt["worker_pid"]
        command = Path(f"/proc/{pid}/cmdline")
        progress["worker_alive"] = command.exists() and str(HERE).encode() in command.read_bytes()
        progress["disk_free"] = shutil.disk_usage(ROOT).free
        progress["accepted"] = (HERE / "acceptance.json").exists()
        print(json.dumps(progress), flush=True)


if __name__ == "__main__":
    main()
