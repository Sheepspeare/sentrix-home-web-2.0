"""Read-only acceptance of the saved album and its media path mapping."""

import argparse
import hashlib
import json
import sqlite3
import tarfile
import time
from collections import Counter
from pathlib import Path

from freeze_album import MEMORY_TABLES, fingerprint, readonly, save, sha


def verify(snapshot, output):
    started = time.time()
    intent = json.loads((snapshot / "intent.json").read_text())
    receipt = json.loads((snapshot / "verification.json").read_text())
    manifest = json.loads((snapshot / "files.json").read_text())
    sources = json.loads((snapshot / "source-sha256.json").read_text())
    data = Path(intent["data"])
    assert receipt["status"] == "completed" and receipt["source_unchanged"]
    assert sha(snapshot / "sentrix.db") == receipt["sqlite_sha256"]
    assert sha(snapshot / "source.tar.gz") == receipt["source_archive_sha256"]
    checked_inodes = set()
    for item in manifest:
        archived = snapshot / item["path"]
        original = data / item["path"]
        stat = archived.stat()
        source_stat = original.stat()
        assert stat.st_size == item["bytes"]
        assert stat.st_mode & 0o222 == 0, item["path"]
        assert (stat.st_dev, stat.st_ino) != (source_stat.st_dev, source_stat.st_ino)
        inode = (stat.st_dev, stat.st_ino)
        if inode not in checked_inodes:
            assert sha(archived) == sha(original) == item["sha256"], item["path"]
            checked_inodes.add(inode)
    assert len(manifest) == receipt["file_count"]
    with tarfile.open(snapshot / "source.tar.gz") as archive:
        assert set(archive.getnames()) == set(sources)
        for member in archive.getmembers():
            with archive.extractfile(member) as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == sources[member.name]
    assert sources["runtime/gateway.py"] == receipt["metadata"]["active_gateway_source_sha256"]
    immutable_uri = (snapshot / "sentrix.db").resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(immutable_uri, uri=True) as saved:
        assert saved.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert fingerprint(saved, MEMORY_TABLES) == receipt["memory_tables"]
        expected = {
            "assets": 13356,
            "observations": 13356,
            "memory_graph_nodes": 71122,
            "memory_graph_edges": 292835,
        }
        assert all(receipt["memory_tables"][table]["count"] == count for table, count in expected.items())
        causal_count = saved.execute(
            "SELECT COUNT(*) FROM memory_graph_edges WHERE relation LIKE 'asset_causes_asset:%'"
        ).fetchone()[0]
        assert causal_count == 23
        paths = {}
        origins = Counter()
        for identifier, path, media_type, derived in saved.execute(
            "SELECT id,path,media_type,derived_kind FROM assets"
        ):
            relative = Path(path).relative_to(data)
            archived = snapshot / relative
            assert archived.is_file(), identifier
            paths[identifier] = str(relative)
            if not derived:
                origins[media_type] += 1
        assert sum(origins.values()) == 11034, dict(origins)
        path_columns = []
        for (table,) in saved.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = saved.execute(f"PRAGMA table_info({quoted})").fetchall()
            path_columns.extend(
                {"table": table, "column": row[1]}
                for row in columns
                if "path" in row[1].lower() or row[1].lower().endswith("_dir")
            )
        vector_groups = saved.execute(
            "SELECT space,model_name,dimension,COUNT(*) FROM memory_vectors GROUP BY 1,2,3"
        ).fetchall()
    with readonly(data / "sentrix.db") as live:
        live.execute("BEGIN")
        assert fingerprint(live, MEMORY_TABLES) == receipt["memory_tables"]
    result = {
        "status": "accepted",
        "snapshot": str(snapshot),
        "sqlite_sha256": receipt["sqlite_sha256"],
        "quick_check": "ok",
        "memory_tables_verified": len(MEMORY_TABLES),
        "source_memories_unchanged": True,
        "files_verified": len(manifest),
        "independent_content_inodes": len(checked_inodes),
        "files_readonly": True,
        "source_archive_members_verified": len(sources),
        "causal_edges": causal_count,
        "original_source_media_types": dict(origins),
        "asset_paths_mapped": len(paths),
        "asset_path_old_root": str(data),
        "path_columns": path_columns,
        "visual_vector_groups": vector_groups,
        "recovery": "Asset.path is absolute. Rebase assets.path on a writable clone before use; "
        "changing only SENTRIX_DATA_DIR does not rewrite persisted paths. "
        "Map each saved path relative to asset_path_old_root into the clone root. "
        "Keep the archive unchanged. Rebuild clone Qdrant from existing memory_vectors.",
        "elapsed_seconds": time.time() - started,
    }
    save(output, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.snapshot, args.output), ensure_ascii=False))
