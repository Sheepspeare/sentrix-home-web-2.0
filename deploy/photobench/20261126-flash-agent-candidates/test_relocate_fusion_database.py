import gzip
import hashlib

import pytest
import relocate_fusion_database as op


def test_exact_archive_preserves_all_bytes_and_refuses_replay(tmp_path):
    original = tmp_path / "original.db"
    data = bytes(range(256)) * 4096
    original.write_bytes(data)
    archive = tmp_path / "archive.db.gz"
    receipt = op.archive_exact(original, archive)
    assert gzip.decompress(archive.read_bytes()) == data == original.read_bytes()
    assert receipt["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["source_size"] == len(data)
    with pytest.raises(AssertionError):
        op.archive_exact(original, archive)


def test_archive_does_not_publish_unverified_compression(tmp_path, monkeypatch):
    original = tmp_path / "original.db"
    original.write_bytes(b"database bytes")
    archive = tmp_path / "archive.db.gz"
    monkeypatch.setattr(op, "digest", lambda path: "wrong")
    with pytest.raises(AssertionError):
        op.archive_exact(original, archive)
    assert original.read_bytes() == b"database bytes" and not archive.exists()
