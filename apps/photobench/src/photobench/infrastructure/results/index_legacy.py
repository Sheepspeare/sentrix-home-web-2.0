"""Explicit legacy indexing: stream JSON into immutable objects before building views.

Run with ``python -m photobench.infrastructure.results.index_legacy --help``.
The source is retained; HTTP readers never launch this conversion themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from photobench.infrastructure.results.split_json import ObjectReader, ObjectWriter, database, manifest, thaw
from photobench.infrastructure.results.split_store import SplitResultWriter
from photobench.infrastructure.results.store import committed_path


def ingest(source: Path, objects: ObjectWriter) -> Any:
    decoder = subprocess.Popen(
        ["gzip", "-dc", str(source)] if source.suffix == ".gz" else ["cat", str(source)],
        stdout=subprocess.PIPE,
    )
    parser = subprocess.Popen(["jq", "--stream", "-c", "."], stdin=decoder.stdout, stdout=subprocess.PIPE)
    assert decoder.stdout is not None and parser.stdout is not None
    decoder.stdout.close()
    frames: list[Any] = []
    root = None

    def assign(container: Any, key: Any, value: Any) -> None:
        if isinstance(container, list):
            if key != len(container):
                raise ValueError("nonsequential JSON list")
            container.append(value)
        else:
            container[key] = value

    try:
        for line in parser.stdout:
            event = json.loads(line)
            path = event[0]
            if len(event) == 2:
                if not path:
                    root = objects.put(event[1])
                    continue
                while len(frames) < len(path):
                    frames.append([] if isinstance(path[len(frames)], int) else {})
                assign(frames[-1], path[-1], objects.put(event[1]))
            else:
                parent = path[:-1]
                if len(frames) != len(parent) + 1:
                    raise ValueError("unexpected JSON container boundary")
                value = objects.put(frames.pop())
                if parent:
                    assign(frames[-1], parent[-1], value)
                else:
                    root = value
        if parser.wait() or decoder.wait() or root is None or frames:
            raise ValueError("legacy JSON stream did not finish")
        return root
    finally:
        parser.stdout.close()
        for process in (parser, decoder):
            if process.poll() is None:
                process.terminate()
            process.wait()


def build_index(root: Path, run_id: str, source: Path | None = None) -> dict[str, Any]:
    directory = root / run_id
    if manifest(directory) is not None:
        raise ValueError("read index already exists")
    source = source or committed_path(directory, "run.json")
    before = source.stat()
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    objects = ObjectWriter(root)
    try:
        ref = ingest(source, objects)
        objects.db.commit()
    finally:
        objects.close()
    state = thaw(ObjectReader(database(root)).value(ref))
    if state["run_id"] != run_id:
        raise ValueError("source run identity mismatch")
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("source checkpoint changed while indexing")
    return SplitResultWriter(root).write(directory, state, source={"path": str(source), "sha256": digest})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if Path(args.run_id).name != args.run_id or args.run_id in {".", ".."}:
        parser.error("invalid run id")
    print(json.dumps(build_index(args.results_root, args.run_id, args.source)))
