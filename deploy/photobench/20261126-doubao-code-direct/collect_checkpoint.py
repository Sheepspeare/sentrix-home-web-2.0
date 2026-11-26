"""Collect frozen checkpoint answers and full traces using GETs only."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import importlib.util
import json
import threading
import time
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def collect(root):
    plan = json.loads((root / "intent.json").read_text())
    op = load("checkpoint_reader", plan["trace_reader"])
    content = load("checkpoint_content", plan["content_reader"])
    lookup = load("checkpoint_lookup", plan["uuid_reader"])
    snapshot = json.loads(Path(plan["snapshot_source"]).read_text())["qa_snapshot"]
    ids = plan["qa_ids"]
    assert len(ids) == len(set(ids)) == 16
    assert not (root / "collection-started.json").exists(), "Inspect saved stages before recovery"
    save(root / "collection-started.json", {"at": time.time(), "agent_requests": 0, "method": "GET"})
    lock = threading.Lock()
    receipts = []
    try:
        for group in plan["groups"]:
            name, port, run = group["group"], group["port"], group["run_id"]
            live = op.get(port, f"runs/{run}?include_history=false")
            assert live["qa_snapshot"] == {k: v for k, v in snapshot.items() if k != "rows"}
            assert live.get("fatal_error") is None and live["build_revision"] == group["build_revision"]
            if group["new_run"]:
                progress = live["phases"]["qa_eval"]["progress"]
                assert progress["dispatch_paused"] and progress["agent_completed"] == 16
            save(root / (name + "-live.json"), live)
            folder = root / name
            folder.mkdir()

            def fetch(qa, port=port, run=run, folder=folder, name=name):
                item, movements = lookup.detail_by_qa(op.get, port, run, qa)
                revision, index = item["trace_storage"]["revision"], item["index"]
                target = folder / qa
                target.mkdir()
                save(target / "item.json", {"item": item})
                hydrated = copy.deepcopy(item)
                paths = {"qa_id", "answer"} | {f["path"] for f in (item.get("content_fields") or [])}
                paths.update(
                    f"runtime_turns.{n}.answer"
                    for n, turn in enumerate(item.get("runtime_turns") or [])
                    if "answer" in turn
                )
                checks = {}
                for path in sorted(paths):
                    value, pages = content.read_content(port, run, index, path, revision, op.get)
                    parent = hydrated
                    parts = path.split(".")
                    for part in parts[:-1]:
                        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                    parent[int(parts[-1]) if isinstance(parent, list) else parts[-1]] = value
                    checks[path] = pages
                for f in hydrated.pop("content_fields", None) or []:
                    value = hydrated
                    for part in f["path"].split("."):
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    assert len(value) == f["characters"]
                assert hydrated["qa_id"] == qa
                save(target / "scoring-item.json", hydrated)
                for filename, path in (
                    ("budget", "runtime_turns.0.trace_bundle.budget"),
                    ("planner", "runtime_turns.0.trace_bundle.planner"),
                    ("metrics", "runtime_turns.0.trace_bundle.model_calls"),
                    ("tools", "tool_trace"),
                    ("agent-trace", "runtime_turns.0.agent2_trace"),
                ):
                    save(target / (filename + ".json"), op.content(port, run, index, path, revision))
                offset, pages = 0, []
                while True:
                    page = op.get(
                        port, f"runs/{run}/items/{index}/trace?offset={offset}&limit=100&revision={revision}"
                    )
                    assert page["read_revision"] == revision
                    assert page.get("qa_id", qa) == qa
                    pages.append(page)
                    next_offset = page.get("next_offset")
                    if next_offset is None:
                        break
                    assert next_offset > offset
                    offset = next_offset
                save(target / "trace-pages.json", pages)
                receipt = {
                    "group": name,
                    "qa_id": qa,
                    "index": index,
                    "revision": revision,
                    "answer_sha256": hashlib.sha256((hydrated.get("answer") or "").encode()).hexdigest(),
                    "content_pages": checks,
                    "trace_pages": len(pages),
                    "index_movements": movements,
                    "execution_status": item.get("execution_status"),
                    "agent_status": item.get("agent_status"),
                }
                save(target / "receipt.json", receipt)
                with lock:
                    receipts.append(receipt)
                    save(
                        root / "collection-progress.json",
                        {
                            "at": time.time(),
                            "completed": len(receipts),
                            "total": len(ids) * len(plan["groups"]),
                        },
                    )
                return qa, hydrated

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                items = dict(pool.map(fetch, ids))
            save(
                root / (name + "-input.json"),
                {"run_id": run, "qa_snapshot": snapshot, "items": [items[q] for q in ids]},
            )
        save(
            root / "collection-receipt.json",
            {"items": receipts, "all_content_and_trace_read_revisions_verified": True, "agent_requests": 0},
        )
        save(
            root / "collection-terminal.json",
            {"status": "completed", "at": time.time(), "items": len(receipts)},
        )
    except Exception as error:
        save(
            root / "collection-terminal.json",
            {"status": "failed", "error_type": type(error).__name__, "error": str(error), "at": time.time()},
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    collect(parser.parse_args().root)
