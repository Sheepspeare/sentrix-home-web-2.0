"""Release retired archive copies after a verified transfer to the local archive."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from archive_recovery_backups import SOURCES, digest, save


def verify_archive(row):
    source = Path(row["archive"])
    original = json.loads(Path(str(source)[:-3] + ".archive.json").read_text())
    assert source.is_file() and not source.is_symlink()
    assert row["local_compressed_verified"] and row["local_decoded_verified"]
    assert Path(row["local_archive"]).is_absolute()
    for field in ("archive", "archive_bytes", "archive_sha256", "source_bytes", "source_sha256"):
        assert row[field] == original[field], field
    signature = source.stat()
    with source.open("rb") as stream:
        assert digest(stream) == (row["archive_bytes"], row["archive_sha256"])
    current = source.stat()
    assert (signature.st_ino, signature.st_size, signature.st_mtime_ns) == (
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    )
    return signature


def run(manifest, output):
    rows = json.loads(manifest.read_text())["archives"]
    expected = {str(path) + ".gz" for path in SOURCES}
    assert len(rows) == len(expected) and {row["archive"] for row in rows} == expected
    signatures = [verify_archive(row) for row in rows]
    opened = subprocess.run(["lsof", "-t", "--", *sorted(expected)], capture_output=True, text=True)
    assert opened.returncode == 1 and not opened.stdout.strip(), "archive is open"
    output.mkdir()
    save(output / "intent.json", {"archives": rows, "at": time.time()})
    completed = []
    try:
        for row, signature in zip(rows, signatures, strict=True):
            source = Path(row["archive"])
            current = source.stat()
            assert (signature.st_ino, signature.st_size, signature.st_mtime_ns) == (
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            )
            receipt = {
                **row,
                "at": time.time(),
                "restore": (
                    "Copy local_archive back to archive, verify archive_sha256, "
                    "then use the original .archive.json restoration procedure."
                ),
            }
            save(source.with_name(source.name + ".offload.json"), receipt)
            source.unlink()
            completed.append(receipt)
            save(output / "progress.json", {"completed": completed})
        result = {"status": "completed", "archives": completed, "at": time.time()}
        save(output / "terminal.json", result)
        return result
    except Exception as error:
        save(output / "terminal.json", {"status": "failed", "completed": completed, "error": str(error)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.verified_manifest, args.output)))
