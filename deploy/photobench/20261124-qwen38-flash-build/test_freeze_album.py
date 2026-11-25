import os
import sqlite3

import freeze_album as freeze
import pytest


def test_online_backup_includes_wal_and_files_are_independent(tmp_path):
    data = tmp_path / "live"
    data.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    media = data / "media"
    media.mkdir()
    original = media / "original.jpg"
    original.write_bytes(b"accepted image")
    os.link(original, media / "same-content.jpg")
    with sqlite3.connect(data / "sentrix.db") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE observations(id TEXT PRIMARY KEY, caption TEXT)")
        writer.execute("INSERT INTO observations VALUES ('o1', 'accepted caption')")
        writer.commit()
        output = tmp_path / "fixed"
        result = freeze.freeze(data, release, output, {}, tables=("observations",))
        with freeze.readonly(output / "sentrix.db") as saved:
            assert saved.execute("SELECT caption FROM observations").fetchone() == ("accepted caption",)
        assert result["source_unchanged"] and result["file_count"] == 2
        assert original.stat().st_ino != (output / "media/original.jpg").stat().st_ino
        assert (output / "media/same-content.jpg").stat().st_ino == (
            output / "media/original.jpg"
        ).stat().st_ino
        (output / "media/original.jpg").write_bytes(b"clone only")
        assert original.read_bytes() == b"accepted image"
        with pytest.raises(FileExistsError):
            freeze.freeze(data, release, output, {}, tables=("observations",))


def test_changed_live_memory_is_not_accepted(tmp_path, monkeypatch):
    data = tmp_path / "live"
    data.mkdir()
    with sqlite3.connect(data / "sentrix.db") as writer:
        writer.execute("CREATE TABLE observations(id TEXT PRIMARY KEY, caption TEXT)")
        writer.execute("INSERT INTO observations VALUES ('o1', 'before')")

    def mutate(*args):
        with sqlite3.connect(data / "sentrix.db") as writer:
            writer.execute("UPDATE observations SET caption = 'changed'")
        return []

    monkeypatch.setattr(freeze, "copy_files", mutate)
    output = tmp_path / "fixed"
    with pytest.raises(ValueError, match="Live memories changed"):
        freeze.freeze(data, tmp_path / "release", output, {}, tables=("observations",))
    assert not (output / "verification.json").exists()


def test_source_archive_excludes_environment_secrets(tmp_path):
    release = tmp_path / "release"
    src = release / "apps/api/src"
    src.mkdir(parents=True)
    (src / "entry.py").write_text("accepted = True\n")
    (src / "secrets.env").write_text("secret=never_archive\n")
    output = tmp_path / "fixed"
    output.mkdir()
    files = freeze.archive_source(release, output)
    assert list(files) == ["apps/api/src/entry.py"]
