"""Reconcile the new evaluator snapshot with the frozen product's reported settings."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("direct_prepare", Path(__file__).with_name("prepare.py"))
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)
FAILED = {
    "limited": "20261126-212812-2026.11.17-atm-sources-addresses-current-61cd30",
    "large": "20261126-212812-2026.11.17-atm-sources-addresses-current-bb56b8",
}


def repair():
    output = op.BASE / "runtime-repair-1"
    output.mkdir()
    before = json.loads((op.BASE / "prepared.json").read_text())["protected_processes_unchanged"]
    op.verify_protected(before)
    source = json.loads((op.BASE / "source-memory-fingerprints.json").read_text())
    evidence = {}
    for group, (product, evaluation, _models, _tools) in op.GROUPS.items():
        run = op.api(evaluation, f"runs/{FAILED[group]}?include_history=false")
        assert run["status"] == "failed"
        assert run["fatal_error"] == "Sentrix effective configuration mismatch: reasoning_max_steps"
        assert "qa_eval" not in run.get("phases", {})
        assert not op.api(evaluation, "active-work")["work"]
        runtime = op.BASE / group / "config/runtime_connection.json"
        settings = json.loads(runtime.read_text())
        assert settings["product_settings"].pop("reasoning_max_steps") == 100
        effective = op.api(product, "diagnostics")["runtime_configuration"]
        assert all(effective.get(key) == value for key, value in settings["product_settings"].items())
        evidence[group] = {
            "failed_run": FAILED[group],
            "agent_qa_requests": 0,
            "runtime_before_sha256": op.sha(runtime),
            "old_run": run,
        }
        op.save(output / (group + "-runtime-before.json"), json.loads(runtime.read_text()))
        op.save(runtime, settings)
        evidence[group]["runtime_after_sha256"] = op.sha(runtime)
    op.save(
        output / "intent.json",
        {
            "at": time.time(),
            "evidence": evidence,
            "agent_code_changes": 0,
            "source_memory_fingerprints": source,
        },
    )
    for group in op.GROUPS:
        subprocess.run(["systemctl", "--user", "restart", f"{op.PREFIX}-{group}-eval.service"], check=True)
    for group, (_product, evaluation, *_rest) in op.GROUPS.items():
        for _ in range(60):
            try:
                config = op.api(evaluation, "config")
                assert "reasoning_max_steps" not in config["runtime_config"]["product_settings"]
                break
            except OSError:
                time.sleep(1)
        else:
            raise TimeoutError(group)
    op.verify_protected(before)
    op.save(
        output / "terminal.json",
        {
            "status": "completed",
            "at": time.time(),
            "evidence": evidence,
            "original_processes_unchanged": before,
            "qa_replayed": False,
        },
    )


if __name__ == "__main__":
    repair()
