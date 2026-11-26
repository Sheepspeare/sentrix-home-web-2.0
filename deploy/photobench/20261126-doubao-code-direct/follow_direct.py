"""Incrementally score saved full-run answers over the direct cloud transport."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import importlib.util
import json
import os
import shlex
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def merge_scores(target, state):
    for result in state.get("scoring_results", {}).values():
        if result["status"] != "completed":
            continue
        diagnostic = state["diagnostic_results"][result["diagnostic_key"]]
        if diagnostic["status"] != "completed":
            continue
        assert result["profile_id"] == "atm_qs_v1" and result["is_evaluation_turn"]
        key = (result["qa_id"], result["turn_index"])
        if key in target:
            assert target[key] == result, (key, "A successful score changed")
        target[key] = result


def missing_ids(order, available, scores):
    done = {qa for qa, _ in scores}
    return [qa for qa in order if qa in available and qa not in done]


def listed_answers(listing):
    """The list endpoint projects saved results; execution_status exists on detail only."""
    return {item["qa_id"]: item for item in listing["items"]}


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=60) as response:
        return json.load(response)


def hydrate(port, run, qa, lookup):
    item, movements = lookup.detail_by_qa(get, port, run, qa)
    result = copy.deepcopy(item)
    revision, index = item["trace_storage"]["revision"], item["index"]
    paths = {"qa_id", "answer"} | {f["path"] for f in item.get("content_fields", [])}
    paths.update(
        f"runtime_turns.{n}.answer" for n, t in enumerate(item.get("runtime_turns") or []) if "answer" in t
    )
    checks = {}
    for path in sorted(paths):
        offset, chunks, pages = 0, [], []
        while True:
            query = urllib.parse.urlencode(
                {"path": path, "offset": offset, "limit": 65536, "revision": revision}
            )
            page = get(port, f"runs/{run}/items/{index}/content?{query}")
            assert page["read_revision"] == revision
            text = page.get("text", "")
            chunks.append(text)
            pages.append(
                {
                    "offset": offset,
                    "read_revision": revision,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
            following = page.get("next_offset")
            if following is None:
                break
            assert following > offset
            offset = following
        value = json.loads("".join(chunks)) if any(chunks) else None
        target, parts = result, path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[int(parts[-1]) if isinstance(target, list) else parts[-1]] = value
        checks[path] = pages
    assert result["qa_id"] == qa and result["execution_status"] == "completed"
    for field in result.pop("content_fields", []):
        value = result
        for part in field["path"].split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert len(value) == field["characters"]
    return result, {
        "qa_id": qa,
        "revision": revision,
        "index": index,
        "content_pages": checks,
        "index_movements": movements,
        "answer_sha256": hashlib.sha256((result["answer"] or "").encode()).hexdigest(),
    }


class Follow:
    def __init__(self, root, secret):
        self.root = root
        self.plan = json.loads((root / "intent.json").read_text())
        self.protocol = json.loads((root / "protocol.json").read_text())
        self.protocol_sha = sha(root / "protocol.json")
        self.lock = threading.RLock()
        self.active, self.maximum, self.progress = {}, {}, {}
        self.key = None
        for line in secret.read_text().splitlines():
            if line.startswith("QWEN_API_KEY="):
                self.key = shlex.split(line.split("=", 1)[1])[0]
        assert self.key and self.protocol["enable_thinking"] is False
        direct = load("direct_endpoint", root / "score_direct_exports.py")
        direct.endpoint(self.protocol["url"])
        os.environ.update(
            FLASH_DIRECT_URL=self.protocol["url"],
            QWEN_API_KEY=self.key,
            FLASH_DIRECT_HTTP_DIR=str(root / "http/raw"),
        )
        load("direct_transport", root / "transport.py").install()
        sys.path.insert(0, str(root / "source"))
        self.scorer = load("frozen_scorer", root / "score_saved.py")
        self.lookup = load("uuid_reader", root / "resume_judge_collection.py")

    def update(self, name, **values):
        with self.lock:
            self.progress.setdefault(name, {}).update(values)
            save(self.root / "progress.json", {"at": time.time(), "groups": self.progress})

    def http(self, name):
        from photobench.infrastructure.http.client import HttpxJsonClient

        owner = self

        class AuditedHttp:
            def __init__(self):
                self.client = HttpxJsonClient()

            def request_json(self, url, payload, method="GET", timeout=180, headers=None):
                assert url == owner.protocol["url"] + "/chat/completions"
                assert payload["model"] == "qwen3.8-flash" and payload["enable_thinking"] is False
                assert payload["temperature"] == 0 and payload["max_tokens"] == 2048
                record = {"group": name, "started_at": time.time(), "input": payload}
                with owner.lock:
                    owner.active[name] = owner.active.get(name, 0) + 1
                    assert owner.active[name] <= 2
                    owner.maximum[name] = max(owner.maximum.get(name, 0), owner.active[name])
                    save(owner.root / "concurrency.json", {"active": owner.active, "maximum": owner.maximum})
                try:
                    response = self.client.request_json(url, payload, method, timeout, headers)
                    assert not response["choices"][0]["message"].get("reasoning_content")
                    record.update(status="completed", response=response)
                    return response
                except Exception as error:
                    record.update(status="failed", error_type=type(error).__name__, error=str(error))
                    raise
                finally:
                    record["finished_at"] = time.time()
                    with owner.lock:
                        with (owner.root / "judge-http.jsonl").open("a") as stream:
                            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        owner.active[name] -= 1
                        save(
                            owner.root / "concurrency.json",
                            {"active": owner.active, "maximum": owner.maximum},
                        )

        return AuditedHttp()

    def group(self, group):
        from photobench.application.run_execution.judge import JudgeRunner

        name, port, run = group["name"], group["port"], group["run_id"]
        folder = self.root / name
        folder.mkdir()
        scores = {}
        previous = self.root / group["previous_state"]
        assert sha(previous) == group["previous_state_sha256"]
        old = json.loads(previous.read_text())
        assert old["run_id"] == run
        merge_scores(scores, old)
        assert len(scores) == 16
        snapshot = old["qa_snapshot"]
        order = [row["qa_id"] for row in snapshot["rows"]]
        assert len(order) == len(set(order)) == 1013
        runner = JudgeRunner(
            http=self.http(name),
            judge_url=self.protocol["url"],
            judge_model=self.protocol["model"],
            judge_api_key=self.key,
            cancel=threading.Event(),
            evaluation_options=self.protocol["evaluation_options"],
        )
        save(folder / "scores.json", list(scores.values()))
        batch_number = 0
        try:
            while True:
                live = get(port, f"runs/{run}?include_history=false")
                assert (
                    live["build_revision"] == self.plan["agent_revision"] and live.get("fatal_error") is None
                )
                assert live["scope_id"] == self.plan["scope_id"]
                assert live["qa_snapshot"] == {k: v for k, v in snapshot.items() if k != "rows"}
                available, page = {}, 1
                while True:
                    listing = get(port, f"runs/{run}/items?page={page}&page_size=100")
                    available.update(listed_answers(listing))
                    if not listing.get("has_next"):
                        break
                    page += 1
                pending = missing_ids(order, available, scores)
                answered = live["phases"]["qa_eval"]["progress"]["agent_completed"]
                self.update(
                    name,
                    status="collecting" if pending else "waiting_for_new_answers",
                    answered=answered,
                    scored=len(scores),
                    valid_mean=sum(r["normalized_score"] for r in scores.values()) / len(scores),
                    observed_missing=len(pending),
                    run_status=live["status"],
                )
                if not pending:
                    if live["status"] in TERMINAL:
                        assert len(available) == answered == len(scores)
                        result = {"status": "completed", "scored": len(scores), "answered": answered}
                        break
                    if time.time() >= self.plan["report_at"]:
                        result = {"status": "report_checkpoint", "scored": len(scores), "answered": answered}
                        break
                    time.sleep(30)
                    continue
                selected = pending[:16]
                batch_number += 1
                batch = folder / f"batch-{batch_number:04d}"
                batch.mkdir()
                collection = batch / "collection"
                collection.mkdir()
                save(
                    batch / "collection-intent.json",
                    {
                        "at": time.time(),
                        "run_id": run,
                        "qa_ids": selected,
                        "protocol_sha256": self.protocol_sha,
                        "method": "GET",
                    },
                )

                def fetch(qa, collection=collection):
                    item, receipt = hydrate(port, run, qa, self.lookup)
                    path = collection / (qa + ".item.json")
                    save(path, item)
                    receipt["item_sha256"] = sha(path)
                    save(collection / (qa + ".receipt.json"), receipt)
                    return qa, item

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    items = dict(pool.map(fetch, selected))
                source = {"run_id": run, "qa_snapshot": snapshot, "items": [items[q] for q in selected]}
                save(batch / "input.json", source)
                save(
                    batch / "intent.json",
                    {
                        "input_sha256": sha(batch / "input.json"),
                        "protocol_sha256": self.protocol_sha,
                        "answers": len(selected),
                        "agent_requests": 0,
                    },
                )
                self.update(name, status="scoring", batch=batch.name, new_batch_count=len(selected))
                for attempt in range(1, 4):
                    result = self.scorer.score_saved(source, self.protocol, batch, runner, concurrency=2)
                    save(batch / f"attempt-{attempt}-terminal.json", result)
                    if result["status"] == "completed":
                        break
                save(batch / "terminal.json", result)
                merge_scores(scores, json.loads((batch / "scoring-state.json").read_text()))
                save(folder / "scores.json", list(scores.values()))
                self.update(
                    name,
                    scored=len(scores),
                    valid_mean=sum(r["normalized_score"] for r in scores.values()) / len(scores),
                    last_batch_result=result,
                )
                if result["status"] != "completed":
                    break
        except Exception as error:
            result = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        assert sha(previous) == group["previous_state_sha256"]
        self.update(name, status=result["status"])
        save(folder / "terminal.json", result)
        if result["status"] not in {"completed", "report_checkpoint"}:
            save(
                self.root / (name + "-attention.json"),
                {"at": time.time(), "group": name, "result": result, "other_group_continues": True},
            )
        return name, result

    def main(self):
        assert not (self.root / "started.json").exists(), "Read checkpoints before explicit recovery"
        manifest = json.loads((self.root / "source-sha256.json").read_text())
        assert all(sha(self.root / relative) == digest for relative, digest in manifest.items())
        save(
            self.root / "started.json",
            {
                "at": time.time(),
                "direct_cloud": True,
                "per_group_concurrency": 2,
                "groups_concurrent": True,
                "agent_requests": 0,
            },
        )
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.group, group) for group in self.plan["groups"]]
            for future in concurrent.futures.as_completed(futures):
                name, result = future.result()
                results[name] = result
                save(self.root / "groups-terminal.json", results)
        status = "completed" if all(r["status"] == "completed" for r in results.values()) else "needs_review"
        save(self.root / "terminal.json", {"at": time.time(), "status": status, "groups": results})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--secret-env", required=True, type=Path)
    args = parser.parse_args()
    try:
        Follow(args.root, args.secret_env).main()
    except BaseException as error:
        save(
            args.root / "failure.json",
            {"at": time.time(), "status": "failed", "error_type": type(error).__name__, "error": str(error)},
        )
        raise
