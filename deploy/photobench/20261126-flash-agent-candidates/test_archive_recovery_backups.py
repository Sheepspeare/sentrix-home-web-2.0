import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "archive_recovery", Path(__file__).with_name("archive_recovery_backups.py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_backup_remains_byte_exact_and_has_restore_receipt(tmp_path):
    source = tmp_path / "old.db"
    original = bytes(range(256)) * 8192
    source.write_bytes(original)
    result = module().archive(source)
    assert not source.exists()
    assert gzip.decompress(Path(result["archive"]).read_bytes()) == original
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert json.loads((tmp_path / "old.db.archive.json").read_text()) == result


def test_decode_failure_preserves_original_backup(tmp_path, monkeypatch):
    m = module()
    source = tmp_path / "old.db"
    source.write_bytes(b"saved recovery data")
    monkeypatch.setattr(m, "digest", lambda stream: (1, "bad hash"))
    with pytest.raises(AssertionError, match="archive mismatch"):
        m.archive(source)
    assert source.read_bytes() == b"saved recovery data"
    assert not (tmp_path / "old.db.gz").exists()
