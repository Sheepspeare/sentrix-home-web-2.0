"""Preserve the remote archive on any unverified or inconsistent transfer."""

import hashlib
import json

import offload_recovery_archives as op
import pytest


def archive_row(tmp_path):
    source = tmp_path / "before.db.gz"
    source.write_bytes(b"complete preserved archive")
    row = {
        "archive": str(source),
        "archive_bytes": source.stat().st_size,
        "archive_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_bytes": 1000,
        "source_sha256": "saved original digest",
    }
    (tmp_path / "before.db.archive.json").write_text(json.dumps(row))
    return {
        **row,
        "local_archive": str(tmp_path / "local/before.db.gz"),
        "local_compressed_verified": True,
        "local_decoded_verified": True,
    }


def test_matching_transfer_is_eligible_without_changing_archive(tmp_path):
    row = archive_row(tmp_path)
    op.verify_archive(row)
    assert (tmp_path / "before.db.gz").read_bytes() == b"complete preserved archive"


@pytest.mark.parametrize("change", ["checksum", "unverified"])
def test_incomplete_transfer_cannot_release_archive(tmp_path, change):
    row = archive_row(tmp_path)
    if change == "checksum":
        (tmp_path / "before.db.gz").write_bytes(b"changed")
    else:
        row["local_decoded_verified"] = False
    with pytest.raises(AssertionError):
        op.verify_archive(row)
    assert (tmp_path / "before.db.gz").is_file()
