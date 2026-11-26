"""Resume saved Judge input collection; resolve live item addresses by UUID."""

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import time
import urllib.parse
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def detail_by_qa(get, port, run, qa):
    """Resolve again only when a concurrent completion moved the live index."""
    moved = []
    for _ in range(3):
        query = urllib.parse.urlencode({"search": qa, "page": 1, "page_size": 10})
        rows = get(port, f"runs/{run}/items?{query}")["items"]
        exact = [row for row in rows if row["qa_id"] == qa]
        assert len(exact) == 1, (qa, "UUID lookup is not unique")
        index = exact[0]["index"]
        detail = get(port, f"runs/{run}/items/{index}")
        item = detail["item"]
        if item["qa_id"] == qa:
            assert item["index"] == index
            return item, moved
        moved.append({"index": index, "observed_qa_id": item["qa_id"]})
    raise AssertionError((qa, "Live index kept moving", moved))


def validate_saved(receipt, item, collector, *, old_score=False):
    assert receipt["qa_id"] == item["qa_id"]
    assert receipt["answer_sha256"] == collector.digest((item["answer"] or "").encode())
    assert receipt["turn_fingerprints"] == [
        collector.answer_fingerprint(turn) for turn in item.get("runtime_turns") or [item]
    ]
    for field in receipt["content_checks"].values():
        assert field["pages"]
        assert all(page["read_revision"] == receipt["revision"] for page in field["pages"])
    if not old_score:
        assert item["trace_storage"]["revision"] == receipt["revision"]
        for path, check in receipt["content_checks"].items():
            value = item
            for part in path.split("."):
                value = value[int(part)] if isinstance(value, list) else value[part]
            assert check["value_sha256"] == collector.digest(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
            )


def collect(source, output):
    assert not output.exists(), "Inspect existing stage receipts before another recovery"
    output.mkdir()
    plan = json.loads((source / "collection-intent.json").read_text())
    spec = importlib.util.spec_from_file_location("saved_collector", source / "collect-inputs.py")
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    save(output / "collection-started.json", {"at": time.time(), "source": str(source), "agent_requests": 0})
    try:

        def group_collect(group):
            name, port, run = group["group"], group["port"], group["run_id"]
            prior = Path(group["previous_state"])
            assert sha(prior) == group["previous_state_sha256"]
            previous = json.loads(prior.read_text())
            assert collector.qa_fingerprint(previous["qa_snapshot"]["rows"]) == plan["definition_sha256"]
            live = collector.OP.get(port, f"runs/{run}?include_history=false")
            assert (
                live["build_revision"] == plan["expected_agent_revision"] and live.get("fatal_error") is None
            )
            old = {r["qa_id"] for r in previous["scoring_results"].values() if r["status"] == "completed"}
            assert len(old) == 83 and old <= set(plan["common_qa_ids"])
            old_items = {item["qa_id"]: item for item in previous["items"]}
            folder = output / "collection" / name
            folder.mkdir(parents=True)
            items, item_paths, receipts, missing = {}, {}, {}, []
            for qa in plan["common_qa_ids"]:
                path = source / "collection" / name / (qa + ".receipt.json")
                if not path.exists():
                    missing.append(qa)
                    continue
                receipt = json.loads(path.read_text())
                assert receipt["reused_score"] == (qa in old)
                item_path = source / "collection" / name / (qa + ".item.json")
                item = old_items[qa] if qa in old else json.loads(item_path.read_text())
                validate_saved(receipt, item, collector, old_score=qa in old)
                if qa in old:
                    collector.validate_old(item, previous["scoring_results"])
                else:
                    items[qa] = item
                    item_paths[qa] = item_path
                receipts[qa] = {"path": str(path), "sha256": sha(path), "reused": True}

            def fetch(qa):
                item, moved = detail_by_qa(collector.OP.get, port, run, qa)
                item, checks = collector.hydrate(item, port, run)
                receipt = {
                    "qa_id": qa,
                    "revision": item["trace_storage"]["revision"],
                    "index": item["index"],
                    "answer_sha256": collector.digest((item["answer"] or "").encode()),
                    "turn_fingerprints": [
                        collector.answer_fingerprint(t) for t in item.get("runtime_turns") or [item]
                    ],
                    "content_checks": checks,
                    "reused_score": qa in old,
                    "index_movements": moved,
                }
                if qa in old:
                    collector.validate_old(item, previous["scoring_results"])
                save(folder / (qa + ".item.json"), item)
                path = folder / (qa + ".receipt.json")
                save(path, receipt)
                return qa, item, {"path": str(path), "sha256": sha(path), "reused": False}

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(fetch, qa) for qa in missing]
                for future in concurrent.futures.as_completed(futures):
                    qa, item, receipt_ref = future.result()
                    if qa not in old:
                        items[qa] = item
                        item_paths[qa] = folder / (qa + ".item.json")
                    receipts[qa] = receipt_ref
                    save(
                        output / (name + "-progress.json"),
                        {"completed": len(receipts), "target": len(plan["common_qa_ids"])},
                    )
            assert set(receipts) == set(plan["common_qa_ids"])
            ids = [q for q in plan["common_qa_ids"] if q not in old]
            input_path = output / (name + "-input.json")
            save(
                input_path,
                {"run_id": run, "qa_snapshot": previous["qa_snapshot"], "items": [items[q] for q in ids]},
            )
            assert sha(prior) == group["previous_state_sha256"]
            save(output / (name + "-receipts.json"), receipts)
            return {
                "group": name,
                "run_id": run,
                "input": input_path.name,
                "sha256": sha(input_path),
                "count": len(ids),
                "reused_previous_scores": len(old),
                "common_total": len(receipts),
                "reused_receipts": len(receipts) - len(missing),
                "new_get_items": len(missing),
                "evidence": [
                    {
                        "qa_id": q,
                        "revision": items[q]["trace_storage"]["revision"],
                        "answer_sha256": collector.digest((items[q]["answer"] or "").encode()),
                        "item_path": str(item_paths[q]),
                    }
                    for q in ids
                ],
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            manifests = list(pool.map(group_collect, plan["groups"]))
        save(output / "inputs.json", manifests)
        save(
            output / "collection-terminal.json",
            {
                "status": "completed",
                "at": time.time(),
                "groups": [{k: v for k, v in group.items() if k != "evidence"} for group in manifests],
                "agent_requests": 0,
                "judge_requests": 0,
            },
        )
    except Exception as error:
        save(
            output / "collection-terminal.json",
            {"status": "failed", "at": time.time(), "error_type": type(error).__name__, "error": str(error)},
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    collect(args.source, args.output)
