"""Export an explicitly disjoint subset of saved answers for incremental judging."""

import argparse
import concurrent.futures
import importlib.util
import json
import time
from pathlib import Path


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(root):
    helper = load_module("resume_collection", Path(__file__).with_name("resume_judge_collection.py"))
    plan = json.loads((root / "collection-intent.json").read_text())
    collector = load_module("original_collector", Path(plan["collector"]))
    ids = plan["qa_ids"]
    assert len(ids) == len(set(ids)) == plan["target"]
    assert set(ids).isdisjoint(plan["already_assigned_qa_ids"])
    assert set(ids) | set(plan["already_assigned_qa_ids"]) == set(plan["snapshot_qa_ids"])
    prior_path = Path(plan["previous_state"])
    assert helper.sha(prior_path) == plan["previous_state_sha256"]
    previous = json.loads(prior_path.read_text())
    assert collector.qa_fingerprint(previous["qa_snapshot"]["rows"]) == plan["definition_sha256"]
    live = collector.OP.get(plan["port"], f"runs/{plan['run_id']}?include_history=false")
    assert live["build_revision"] == plan["agent_revision"] and live.get("fatal_error") is None
    assert not (root / "collection-started.json").exists(), "Inspect existing receipts before recovery"
    helper.save(
        root / "collection-started.json", {"at": time.time(), "http_method": "GET", "agent_requests": 0}
    )
    folder = root / "collection"
    folder.mkdir()
    try:

        def fetch(qa):
            item, moved = helper.detail_by_qa(collector.OP.get, plan["port"], plan["run_id"], qa)
            item, checks = collector.hydrate(item, plan["port"], plan["run_id"])
            path = folder / (qa + ".item.json")
            helper.save(path, item)
            receipt = {
                "qa_id": qa,
                "revision": item["trace_storage"]["revision"],
                "index": item["index"],
                "answer_sha256": collector.digest((item["answer"] or "").encode()),
                "content_checks": checks,
                "item_sha256": helper.sha(path),
                "index_movements": moved,
            }
            helper.save(folder / (qa + ".receipt.json"), receipt)
            return qa, item, {**receipt, "item_path": str(path)}

        items, evidence = {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fetch, qa) for qa in ids]
            for future in concurrent.futures.as_completed(futures):
                qa, item, receipt = future.result()
                items[qa], evidence[qa] = item, receipt
                helper.save(
                    root / "collection-progress.json",
                    {"at": time.time(), "completed": len(items), "target": len(ids)},
                )
        input_path = root / "baseline-limited-input.json"
        helper.save(
            input_path,
            {
                "run_id": plan["run_id"],
                "qa_snapshot": previous["qa_snapshot"],
                "items": [items[q] for q in ids],
            },
        )
        assert helper.sha(prior_path) == plan["previous_state_sha256"]
        helper.save(
            root / "inputs.json",
            [
                {
                    "group": "baseline-limited",
                    "input": input_path.name,
                    "sha256": helper.sha(input_path),
                    "count": len(ids),
                    "evidence": [evidence[q] for q in ids],
                }
            ],
        )
        helper.save(
            root / "collection-terminal.json",
            {
                "status": "completed",
                "at": time.time(),
                "count": len(ids),
                "agent_requests": 0,
                "judge_requests": 0,
            },
        )
    except Exception as error:
        helper.save(
            root / "collection-terminal.json",
            {"status": "failed", "at": time.time(), "error_type": type(error).__name__, "error": str(error)},
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    collect(parser.parse_args().root)
