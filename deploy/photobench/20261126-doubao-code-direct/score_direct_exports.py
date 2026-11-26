"""Score saved exports in an isolated process making direct cloud requests."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def endpoint(url):
    parsed = urlsplit(url)
    assert parsed.scheme == "https" and parsed.hostname == "llm-355ba48p9iw7pkjz.cn-beijing.maas.aliyuncs.com"
    assert parsed.path.rstrip("/") == "/compatible-mode/v1"
    return url.rstrip("/")


def main(root, secret):
    manifest = json.loads((root / "source-sha256.json").read_text())
    for relative, digest in manifest.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
    sys.path.insert(0, str(root / "source"))
    spec = importlib.util.spec_from_file_location("frozen_scorer", root / "score_saved.py")
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    from photobench.application.run_execution.judge import JudgeRunner
    from photobench.infrastructure.http.client import HttpxJsonClient

    protocol_bytes = (root / "protocol.json").read_bytes()
    protocol = json.loads(protocol_bytes)
    url = endpoint(protocol["url"])
    key = None
    for line in secret.read_text().splitlines():
        if line.startswith("QWEN_API_KEY="):
            key = shlex.split(line.split("=", 1)[1])[0]
    assert key and protocol["enable_thinking"] is False
    inputs = json.loads((root / "inputs.json").read_text())
    assert not (root / "started.json").exists(), "Inspect successes before explicit recovery"
    os.environ.update(FLASH_DIRECT_URL=url, QWEN_API_KEY=key, FLASH_DIRECT_HTTP_DIR=str(root / "http/raw"))
    transport_spec = importlib.util.spec_from_file_location("direct_transport", root / "transport.py")
    transport = importlib.util.module_from_spec(transport_spec)
    transport_spec.loader.exec_module(transport)
    transport.install()
    lock = threading.Lock()
    active, maximum = {}, {}
    save = scorer.save

    class AuditedHttp:
        def __init__(self, group):
            self.group, self.client = group, HttpxJsonClient()

        def request_json(self, request_url, payload, method="GET", timeout=180, headers=None):
            assert request_url == url + "/chat/completions"
            assert payload["model"] == "qwen3.8-flash" and payload["enable_thinking"] is False
            assert payload["temperature"] == 0 and 0 < payload["max_tokens"] <= 2048
            record = {"group": self.group, "started_at": time.time(), "input": payload}
            with lock:
                active[self.group] = active.get(self.group, 0) + 1
                assert active[self.group] <= 2
                maximum[self.group] = max(maximum.get(self.group, 0), active[self.group])
                save(root / "concurrency.json", {"active": active, "maximum": maximum})
            try:
                response = self.client.request_json(request_url, payload, method, timeout, headers)
                record.update(status="completed", response=response)
                assert not response["choices"][0]["message"].get("reasoning_content")
                return response
            except Exception as error:
                record.update(status="failed", error_type=type(error).__name__, error=str(error))
                raise
            finally:
                record["finished_at"] = time.time()
                with lock:
                    with (root / "judge-http.jsonl").open("a") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    active[self.group] -= 1
                    save(root / "concurrency.json", {"active": active, "maximum": maximum})

    def score(group):
        name = group["group"]
        blob = (root / group["input"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == group["sha256"]
        source = json.loads(blob)
        output = root / name
        output.mkdir()
        save(
            output / "intent.json",
            {
                "source_run_id": source["run_id"],
                "input_sha256": group["sha256"],
                "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
                "answers": len(source["items"]),
                "agent_requests": 0,
            },
        )
        try:
            runner = JudgeRunner(
                http=AuditedHttp(name),
                judge_url=url,
                judge_model=protocol["model"],
                judge_api_key=key,
                cancel=threading.Event(),
                evaluation_options=protocol["evaluation_options"],
            )
            result = scorer.score_saved(source, protocol, output, runner, concurrency=2)
        except Exception as error:
            result = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        save(output / "terminal.json", result)
        return name, result

    save(
        root / "started.json",
        {
            "at": time.time(),
            "direct_cloud": True,
            "per_group_concurrency": 2,
            "groups_concurrent": True,
            "agent_requests": 0,
        },
    )
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(inputs)) as pool:
        for name, result in pool.map(score, inputs):
            results[name] = result
            save(root / "progress.json", {"at": time.time(), "groups": results})
    good = all(result["status"] == "completed" for result in results.values())
    save(
        root / "terminal.json",
        {
            "status": "completed" if good else "needs_review",
            "at": time.time(),
            "groups": results,
            "agent_requests": 0,
            "direct_cloud": True,
        },
    )
    return 0 if good else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--secret-env", type=Path, required=True)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.root, args.secret_env))
    except Exception as error:
        (args.root / "failure.json").write_text(
            json.dumps({"error_type": type(error).__name__, "error": str(error)})
        )
        raise
