"""Judge newly saved answers while the original QA runs continue unchanged."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def merge_scores(target, state):
    """Include validated completed scores, keeping missing results out of the mean."""
    for result in state.get("scoring_results", {}).values():
        if result["status"] != "completed":
            continue
        assert result["profile_id"] == "atm_qs_v1"
        diagnostic = state["diagnostic_results"][result["diagnostic_key"]]
        assert diagnostic["status"] == "completed"
        key = (result["qa_id"], result["turn_index"])
        if key in target:
            assert target[key] == result, (key, "different scores for the same saved answer")
        target[key] = result


def missing_ids(order, available, scores):
    scored = {qa for qa, _turn in scores}
    return [qa for qa in order if qa in available and qa not in scored]


class Follow:
    def __init__(self, root):
        self.root = root
        self.plan = json.loads((root / "intent.json").read_text())
        self.protocol = json.loads((root / "protocol.json").read_text())
        self.protocol_sha = sha(root / "protocol.json")
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active, self.maximum = {}, {}
        self.progress = {}
        sys.path.insert(0, str(Path(self.plan["repo"]) / "apps/photobench/src"))
        self.scorer = load("follow_scorer", self.plan["scorer"])
        self.collector = load("follow_collector", self.plan["collector"])
        self.lookup = load("follow_lookup", self.plan["lookup"])
        self.save = self.scorer.save

    def update(self, name, **values):
        with self.lock:
            self.progress.setdefault(name, {}).update(values)
            self.save(self.root / "progress.json", {"at": time.time(), "groups": self.progress})

    def http(self, name):
        from photobench.infrastructure.http.client import HttpxJsonClient

        owner = self

        class AuditedHttp:
            def __init__(self):
                self.client = HttpxJsonClient()

            def request_json(self, url, payload, method="GET", timeout=180, headers=None):
                assert url == owner.protocol["url"] + "/chat/completions"
                assert payload["model"] == "qwen3.8-flash"
                assert payload["enable_thinking"] is False
                assert payload["temperature"] == 0 and payload["max_tokens"] <= 2048
                record = {"group": name, "started_at": time.time(), "input": payload}
                with owner.lock:
                    owner.active[name] = owner.active.get(name, 0) + 1
                    assert owner.active[name] <= 2
                    owner.maximum[name] = max(owner.maximum.get(name, 0), owner.active[name])
                    owner.save(
                        owner.root / "concurrency.json",
                        {"active": owner.active, "maximum": owner.maximum},
                    )
                try:
                    response = self.client.request_json(url, payload, method, timeout, headers)
                    record.update(status="completed", response=response)
                    message = (response.get("choices") or [{}])[0].get("message") or {}
                    assert not message.get("reasoning_content"), "Unexpected thinking output"
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
                        owner.save(
                            owner.root / "concurrency.json",
                            {"active": owner.active, "maximum": owner.maximum},
                        )

        return AuditedHttp()

    def group(self, group):
        from photobench.application.run_execution.judge import JudgeRunner

        name, port, run = group["name"], group["port"], group["run_id"]
        folder = self.root / name
        folder.mkdir()
        scores, snapshot = {}, None
        for record in group["previous_states"]:
            assert sha(record["path"]) == record["sha256"]
            state = json.loads(Path(record["path"]).read_text())
            assert state["run_id"] == run
            if snapshot is None:
                snapshot = state["qa_snapshot"]
            assert snapshot == state["qa_snapshot"]
            merge_scores(scores, state)
        assert len(scores) == group["previous_scored"]
        order = [row["qa_id"] for row in snapshot["rows"]]
        assert len(order) == len(set(order)) == 1013
        runner = JudgeRunner(
            http=self.http(name),
            judge_url=self.protocol["url"],
            judge_model=self.protocol["model"],
            cancel=threading.Event(),
            evaluation_options=self.protocol["evaluation_options"],
        )
        batch_number = 0
        try:
            while not self.stop.is_set() and time.time() < self.plan["stop_at"]:
                live = self.collector.OP.get(port, f"runs/{run}?include_history=false")
                assert live["build_revision"] == self.plan["agent_revision"]
                assert live.get("fatal_error") is None
                assert live["qa_snapshot"] == {k: v for k, v in snapshot.items() if k != "rows"}
                available, page = {}, 1
                while True:
                    value = self.collector.OP.get(port, f"runs/{run}/items?page={page}&page_size=100")
                    available.update({item["qa_id"]: item for item in value["items"]})
                    if not value.get("has_next"):
                        break
                    page += 1
                pending = missing_ids(order, available, scores)
                count = live["phases"]["qa_eval"]["progress"]["agent_completed"]
                self.update(
                    name,
                    status="collecting" if pending else "waiting_for_new_answers",
                    answered=count,
                    scored=len(scores),
                    valid_mean=sum(r["normalized_score"] for r in scores.values()) / len(scores),
                    observed_missing=len(pending),
                    run_status=live["status"],
                )
                if not pending:
                    if live["status"] in {
                        "completed",
                        "completed_with_errors",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        assert len(available) == count, "Refresh inconsistent terminal listing"
                        break
                    self.stop.wait(self.plan["poll_seconds"])
                    continue
                selected = pending[: self.plan["batch_size"]]
                batch_number += 1
                batch = folder / f"batch-{batch_number:04d}"
                batch.mkdir()
                collection = batch / "collection"
                collection.mkdir()
                self.save(
                    batch / "collection-intent.json",
                    {"run_id": run, "qa_ids": selected, "protocol_sha256": self.protocol_sha},
                )

                def fetch(qa, collection=collection):
                    item, movements = self.lookup.detail_by_qa(self.collector.OP.get, port, run, qa)
                    item, checks = self.collector.hydrate(item, port, run)
                    item_path = collection / f"{qa}.item.json"
                    self.save(item_path, item)
                    self.save(
                        collection / f"{qa}.receipt.json",
                        {
                            "qa_id": qa,
                            "revision": item["trace_storage"]["revision"],
                            "answer_sha256": self.collector.digest((item["answer"] or "").encode()),
                            "item_sha256": sha(item_path),
                            "content_checks": checks,
                            "index_movements": movements,
                        },
                    )
                    return qa, item

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    items = dict(pool.map(fetch, selected))
                source = {"run_id": run, "qa_snapshot": snapshot, "items": [items[q] for q in selected]}
                self.save(batch / "input.json", source)
                self.save(
                    batch / "intent.json",
                    {
                        "input_sha256": sha(batch / "input.json"),
                        "protocol_sha256": self.protocol_sha,
                        "answers": len(selected),
                        "agent_requests": 0,
                    },
                )
                self.update(name, status="scoring", batch=batch.name, new_batch_count=len(selected))
                result = self.scorer.score_saved(source, self.protocol, batch, runner, concurrency=2)
                self.save(batch / "terminal.json", result)
                merge_scores(scores, json.loads((batch / "scoring-state.json").read_text()))
                self.save(folder / "scores.json", list(scores.values()))
                self.update(
                    name,
                    scored=len(scores),
                    valid_mean=sum(r["normalized_score"] for r in scores.values()) / len(scores),
                    last_batch_result=result,
                )
                if result["status"] != "completed":
                    self.stop.set()
                    self.save(folder / "terminal.json", result)
                    return name, result
            result = {
                "status": "completed" if not self.stop.is_set() else "paused_after_other_group_failure",
                "scored": len(scores),
                "valid_mean": sum(r["normalized_score"] for r in scores.values()) / len(scores),
                "reason": "original_run_terminal_or_report_cutoff",
            }
        except Exception as error:
            self.stop.set()
            result = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        self.save(folder / "terminal.json", result)
        return name, result

    def main(self):
        assert not (self.root / "started.json").exists(), "Inspect successful stages before recovery"
        for record in self.plan["source_files"]:
            assert sha(record["path"]) == record["sha256"]
        self.save(
            self.root / "started.json",
            {"at": time.time(), "per_group_concurrency": 2, "groups_concurrent": True, "agent_requests": 0},
        )
        tunnel = subprocess.Popen(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=30",
                "-N",
                "-L",
                "127.0.0.1:18158:127.0.0.1:18148",
                "192.168.0.100",
            ],
            stdout=subprocess.DEVNULL,
            stderr=(self.root / "tunnel.stderr").open("x"),
        )
        try:
            for _ in range(40):
                assert tunnel.poll() is None, "Independent SSH tunnel exited"
                try:
                    with urllib.request.urlopen("http://127.0.0.1:18158/health", timeout=2) as response:
                        health = json.load(response)
                    assert health["max_concurrency"] == 24 and health["enable_thinking"] is False
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise TimeoutError("Independent SSH tunnel did not become ready")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = dict(pool.map(self.group, self.plan["groups"]))
            for group in self.plan["groups"]:
                for record in group["previous_states"]:
                    assert sha(record["path"]) == record["sha256"]
            good = all(result["status"] == "completed" for result in results.values())
            self.save(
                self.root / "terminal.json",
                {"status": "completed" if good else "needs_review", "at": time.time(), "groups": results},
            )
            return 0 if good else 1
        finally:
            tunnel.terminate()
            tunnel.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    follow = Follow(args.root)
    try:
        raise SystemExit(follow.main())
    except Exception as error:
        follow.save(
            args.root / "terminal.json",
            {"status": "failed", "at": time.time(), "error_type": type(error).__name__, "error": str(error)},
        )
        raise
