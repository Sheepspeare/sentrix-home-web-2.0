"""Deploy the reviewed large candidate, verify it, then run eleven frozen questions."""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

BASE = "/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-agent-candidates-20261126"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "192.168.0.100"]
HOST = "192.168.0.100"


def run(revision, audit, repo):
    spec = importlib.util.spec_from_file_location(
        "candidate_watch", Path(__file__).with_name("watch_candidates.py")
    )
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
    deployment = audit / "deployment"
    deployment.mkdir()
    watch.save(deployment / "intent.json", {"revision": revision, "group": "large", "checkpoint": 11})
    ops = f"{BASE}/release-{revision[:8]}/deploy/photobench/20261126-flash-agent-candidates"

    def remote(command):
        return subprocess.check_output([*SSH, command], text=True)

    def fetch(name, target=None):
        value = json.loads(remote(f"cat {BASE}/large-v3/{name}"))
        watch.save(deployment / (target or name), value)
        return value

    subprocess.run(
        [*SSH, f"{PYTHON} {ops}/revise_limited.py --revision {revision} --group large --stage v3"], check=True
    )
    verification = fetch("verification.json")
    fetch("answer-preservation.json")
    assert verification["revision"] == revision
    assert verification["memory_18_tables_unchanged"] and verification["old_eight_answers_preserved"]
    remote(
        f"{PYTHON} {ops}/verify_group_pages.py --release {BASE}/release-{revision[:8]} "
        f"--database {BASE}/large/data/sentrix.db "
        "--scope PhotoBench-20261124-221506-2026.11.17-atm-sources-addresses-qwen3.8-flash "
        f"--output {BASE}/large-v3/group-pagination-verification.json"
    )
    groups = fetch("group-pagination-verification.json")
    assert groups["all_pages_equal_full_production_answer"] and groups["groups"] == 1660
    assert groups["model_requests"] == 0 and groups["max_model_visible_chars"] < 24000
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=repo, check=True)
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()
    pushed = subprocess.check_output(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=repo, text=True
    )
    assert pushed.split()[0] == revision
    watch.save(deployment / "push-receipt.json", {"revision": revision, "branch": branch, "verified": True})
    for source, destination in (
        (audit / "large-run-payload.json", f"{BASE}/large/run-payload.json"),
        (audit / "dispatch-control.json", f"{BASE}/large-v3/dispatch-control.json"),
    ):
        subprocess.run(["scp", "-q", str(source), f"{HOST}:{destination}.tmp"], check=True)
        remote(f"mv {destination}.tmp {destination}")
    return watch.run("large", audit, audit / "first-eleven", target=11)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    sys.exit(run(args.revision, args.audit, args.repo))
