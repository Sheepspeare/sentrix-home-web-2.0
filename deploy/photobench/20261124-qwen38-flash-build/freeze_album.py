"""Archive the accepted album without opening its live embedded Qdrant index."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import time
import urllib.request
from pathlib import Path

MEMORY_TABLES = (
    "assets",
    "observations",
    "events",
    "memory_vectors",
    "text_vectors",
    "facts",
    "entities",
    "entity_properties",
    "relationships",
    "memory_graph_nodes",
    "memory_graph_edges",
    "entity_observations",
    "event_entities",
    "entity_mentions",
    "event_observations",
    "observation_search_terms",
    "observation_search_fts",
    "memory_graph_builds",
)
FILE_DIRS = ("media", "video-jobs", "previews", "geo")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def fingerprint(connection, tables):
    result = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        if not columns:
            raise ValueError(f"Missing memory table: {table}")
        keys = [row[1] for row in sorted(columns, key=lambda r: r[5]) if row[5]]
        order = ",".join('"' + key.replace('"', '""') + '"' for key in keys) or "rowid"
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
            values = [value.hex() if isinstance(value, bytes) else value for value in row]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
            count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def copy_files(data, output):
    manifest = []
    copied = {}
    for directory in FILE_DIRS:
        source_dir = data / directory
        if not source_dir.is_dir():
            continue
        (output / directory).mkdir()
        for source in sorted(source_dir.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(data)
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            inode = (source.stat().st_dev, source.stat().st_ino)
            if inode in copied:
                prior, digest = copied[inode]
                os.link(prior, target)
            else:
                shutil.copy2(source, target)
                digest = sha(target)
                if sha(source) != digest:
                    raise ValueError(f"Source changed during copy: {relative}")
                copied[inode] = (target, digest)
            manifest.append({"path": str(relative), "bytes": target.stat().st_size, "sha256": digest})
    return manifest


def archive_source(release, output):
    files = {}
    roots = [release / "apps/api/src", release / "apps/photobench/src", release / "packages"]
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for root in roots:
            for source in sorted(root.rglob("*")):
                if not source.is_file() or any(
                    part in {"__pycache__", "node_modules", ".git", ".venv"} for part in source.parts
                ):
                    continue
                if source.suffix not in {".py", ".sql", ".ts", ".tsx", ".toml"}:
                    continue
                relative = str(source.relative_to(release))
                files[relative] = sha(source)
                archive.add(source, arcname=relative, recursive=False)
        for name in ("pyproject.toml", "build-info.json"):
            source = release / name
            if source.is_file():
                files[name] = sha(source)
                archive.add(source, arcname=name, recursive=False)
        gateway = release.parent / "gateway.py"
        if gateway.is_file():
            files["runtime/gateway.py"] = sha(gateway)
            archive.add(gateway, arcname="runtime/gateway.py", recursive=False)
    return files


def assert_idle(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        state = json.load(response)
    if state.get("work"):
        raise ValueError("Evaluation is active; snapshot not started")
    return state


def freeze(data, release, output, metadata, *, tables=MEMORY_TABLES):
    started = time.time()
    output.mkdir(parents=True, exist_ok=False)
    save(output / "intent.json", {"started_at": started, "data": str(data), "metadata": metadata})
    with readonly(data / "sentrix.db") as source, sqlite3.connect(output / "sentrix.db") as target:
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Snapshot SQLite quick_check failed")
        frozen = fingerprint(target, tables)
    files = copy_files(data, output)
    source_files = archive_source(release, output)
    with readonly(data / "sentrix.db") as source:
        source.execute("BEGIN")
        current = fingerprint(source, tables)
    if current != frozen:
        raise ValueError("Live memories changed during snapshot; inspect this unaccepted archive")
    save(output / "files.json", files)
    save(output / "source-sha256.json", source_files)
    save(output / "memory-fingerprints.json", frozen)
    (output / "RESTORE.md").write_text(
        "# Frozen album\n\n"
        "Copy sentrix.db and the four media/job directories into a separate writable data directory. "
        "Keep the archive read-only; run evaluations on a clone. Source code is in source.tar.gz. "
        "The SQLite backup includes the authoritative visual and text vectors, lexical indexes and graph.\n\n"
        "The live embedded Qdrant cache was intentionally not copied. Before an evaluation clone is used, "
        "rebuild its own visual Qdrant cache from SQLite memory_vectors with the archived production "
        "QdrantVectorIndex.upsert, keeping each row's space/source_type/source_id/model_name/metadata "
        "and vector_json. Do not re-embed or call a generation model. Text retrieval uses text_vectors. "
        "Verify counts and retrieval on the clone before QA. Never open the live Qdrant directory "
        "from a second process. Runtime secrets are supplied separately and are absent here.\n"
    )
    receipt = {
        "status": "completed",
        "snapshot": str(output),
        "metadata": metadata,
        "sqlite_sha256": sha(output / "sentrix.db"),
        "source_archive_sha256": sha(output / "source.tar.gz"),
        "file_count": len(files),
        "file_bytes": sum(item["bytes"] for item in files),
        "memory_tables": frozen,
        "source_unchanged": True,
        "qdrant": "derived cache excluded; rebuild from saved authoritative vectors before clone QA",
        "elapsed_seconds": time.time() - started,
    }
    save(output / "verification.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--idle-url", required=True)
    args = parser.parse_args()
    assert_idle(args.idle_url)
    result = freeze(args.data, args.release, args.output, json.loads(args.metadata.read_text()))
    assert_idle(args.idle_url)
    # Only independent archive files are made read-only; the live data is never chmod'ed.
    for path in args.output.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    save(args.output.parent / (args.output.name + "-terminal.json"), result)
    print(json.dumps({key: result[key] for key in ("status", "snapshot", "file_count", "elapsed_seconds")}))


if __name__ == "__main__":
    main()
