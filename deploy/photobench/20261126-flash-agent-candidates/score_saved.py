"""Score immutable answer exports with the production scoring implementation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import threading
from pathlib import Path

from photobench.application.config.service import ConfigService
from photobench.application.run_execution.judge import JudgeRunner
from photobench.application.run_execution.profile_scoring import ProfileScoring, prepare_batches
from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
from photobench.domain.evaluation.profiles import scoring_targets, summarize_batches
from photobench.infrastructure.config.files import JsonConfigStore
from photobench.infrastructure.http.client import HttpxJsonClient


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def score_saved(source, protocol, output, runner):
    """Checkpoint each production request; completed results survive an explicit resume."""
    original = copy.deepcopy(source)
    snapshot = source["qa_snapshot"]
    assert qa_fingerprint(snapshot["rows"]) == snapshot["definition_sha256"]
    assert protocol["judge_use_gt_images"] is False
    assert all(
        not config["use_gt_images"] and not config["use_gt_metadata"]
        for config in protocol["scoring_configs"].values()
    )
    state_file = output / "scoring-state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else copy.deepcopy(source)
    assert state["qa_snapshot"] == snapshot and state["items"] == source["items"]
    lock = threading.RLock()

    def checkpoint():
        with lock:
            save(state_file, state)

    batches = prepare_batches(
        state,
        protocol["scoring_configs"],
        {"provider_id": protocol["provider_id"], "url": protocol["url"], "model": protocol["model"]},
        nonce="frozen-answer-export-v1",
    )
    assert all(batch["config"] == protocol["scoring_configs"][batch["profile_id"]] for batch in batches)
    service = ProfileScoring(runner, state, lock, checkpoint, {})
    checkpoint()
    completed = 0
    for batch in batches:
        for target in scoring_targets(state, batch, "missing"):
            if target["key"] not in batch["targets"]:
                batch["targets"].append(target["key"])
            result = service.score(batch, target)
            completed += 1
            diagnostic = state.get("diagnostic_results", {}).get(result.get("diagnostic_key"), {})
            checkpoint()
            if result["status"] != "completed" or diagnostic.get("status") != "completed":
                assert source == original
                return {
                    "status": "needs_review",
                    "reason": "judge_request_or_validation_failed",
                    "scored_this_invocation": completed,
                    "qa_id": target["qa_id"],
                }
    state["scoring_summaries"] = summarize_batches(state)
    checkpoint()
    assert source == original
    return {
        "status": "completed",
        "scored_this_invocation": completed,
        "summaries": state["scoring_summaries"],
    }


def main(args):
    source_bytes = args.input.read_bytes()
    protocol_bytes = args.protocol.read_bytes()
    source, protocol = json.loads(source_bytes), json.loads(protocol_bytes)
    if args.resume:
        assert (args.output / "intent.json").exists()
        old = json.loads((args.output / "intent.json").read_text())
        assert old["input_sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert old["protocol_sha256"] == hashlib.sha256(protocol_bytes).hexdigest()
    else:
        args.output.mkdir()
        save(
            args.output / "intent.json",
            {
                "source_run_id": source["run_id"],
                "input_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
                "answers": len(source["items"]),
                "agent_requests": 0,
                "mode": "independent_scoring_export; original_run_unchanged",
            },
        )
    for raw in args.secret_env.read_text().splitlines():
        if raw.strip() and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            tokens = shlex.split(value)
            os.environ[key] = tokens[0] if len(tokens) == 1 else value
    config = ConfigService(JsonConfigStore(args.config_root))
    _, url, model, key = config.resolve_judge_provider(protocol["provider_id"])
    assert url.rstrip("/") == protocol["url"].rstrip("/") and model == protocol["model"]
    runner = JudgeRunner(
        http=HttpxJsonClient(), judge_url=url, judge_model=model, judge_api_key=key, cancel=threading.Event()
    )
    try:
        terminal = score_saved(source, protocol, args.output, runner)
    except Exception as exc:
        save(
            args.output / "terminal.json",
            {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
        )
        raise
    save(args.output / "terminal.json", terminal)
    print(json.dumps({k: v for k, v in terminal.items() if k != "summaries"}, ensure_ascii=False))
    return int(terminal["status"] != "completed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--secret-env", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    raise SystemExit(main(parser.parse_args()))
