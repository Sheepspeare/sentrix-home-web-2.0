"""Revise only the paused fusion instance, retaining its album and accepted answers."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import prepare_fusion
import prepare_parallel
import revise_limited

STAGE = "compact-query"


def protected_processes():
    protected = {}
    units = subprocess.check_output(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", "sentrix*.service"],
        text=True,
    )
    own = {f"{prepare_fusion.PREFIX}-large-{role}.service" for role in ("api", "eval")}
    for line in units.splitlines():
        name = next((part for part in line.split() if part.endswith(".service")), "")
        if not name or name in own:
            continue
        pid = subprocess.check_output(
            ["systemctl", "--user", "show", name, "--property=MainPID", "--value"], text=True
        ).strip()
        if int(pid) > 0:
            protected[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
    for pid in (4191434, 2427495, 2427487):
        path = Path(f"/proc/{pid}/stat")
        if path.exists():
            protected[str(pid)] = path.read_text().split()[21]
    assert len(protected) >= 10
    return protected


def run(
    revision,
    release,
    *,
    continue_archived=False,
    stage=STAGE,
    previous_run="20261126-215538-2026.11.17-atm-sources-addresses-big_model-7dc9ca",
    previous_revision="42031dfa4326cc1a5943f53d04c22f9230787b94",
    previous_release="release",
    accepted="fusion1",
):
    base = prepare_fusion.BASE
    composed = base / f"release-{revision[:8]}"
    if not composed.exists():
        evaluation = base / "release/apps/photobench"
        prepare_parallel.compose_release(release, evaluation.resolve(), composed)
    candidate = revise_limited.candidate
    candidate.BASE = base
    candidate.PREFIX = prepare_fusion.PREFIX
    candidate.GROUPS = {"large": prepare_fusion.PORTS}
    candidate.protected_processes = protected_processes
    revise_limited.BASE = base
    revise_limited.STAGES[stage] = {
        "runs": {"large": previous_run},
        "previous_revision": previous_revision,
        "previous_release": previous_release,
        "accepted": accepted,
        "answer_count": 16,
        "answer_word": "sixteen",
    }
    revise_limited.run(revision, group="large", stage=stage, continue_archived=continue_archived, workers=8)
    payload_path = base / "large/run-payload.json"
    payload = json.loads(payload_path.read_text())
    payload["qa_concurrency"] = 8
    candidate.helper.save(payload_path, payload)
    candidate.helper.save(
        base / "large/dispatch-control.json", {"concurrency": 8, "pause_after_completed": 16}
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--continue-archived", action="store_true")
    parser.add_argument("--stage", default=STAGE)
    parser.add_argument("--previous-run")
    parser.add_argument("--previous-revision")
    parser.add_argument("--previous-release")
    parser.add_argument("--accepted")
    args = parser.parse_args()
    previous = {
        key: value
        for key, value in {
            "previous_run": args.previous_run,
            "previous_revision": args.previous_revision,
            "previous_release": args.previous_release,
            "accepted": args.accepted,
        }.items()
        if value is not None
    }
    run(args.revision, args.release, stage=args.stage, continue_archived=args.continue_archived, **previous)
