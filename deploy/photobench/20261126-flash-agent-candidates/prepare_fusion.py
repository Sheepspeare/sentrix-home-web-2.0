"""Prepare an independent condition-fusion instance without changing existing services."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import prepare_parallel

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-condition-fusion-20261126")
PREFIX = "sentrix-flash-condition-fusion"
PORTS = (8303, 8883, 100, 600)
_original_candidate_module = prepare_parallel.candidate_module


def protected_processes(original):
    protected = original()
    units = subprocess.check_output(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", "sentrix-flash*.service"],
        text=True,
    )
    for line in units.splitlines():
        name = line.split()[0]
        pid = subprocess.check_output(
            ["systemctl", "--user", "show", name, "--property=MainPID", "--value"], text=True
        ).strip()
        if int(pid) > 0:
            protected[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
    return protected


def candidate_module():
    module = _original_candidate_module()
    original = module.protected_processes
    module.protected_processes = lambda: protected_processes(original)
    return module


def run(release, payload, *, resume=False):
    prepare_parallel.BASE = BASE
    prepare_parallel.PREFIX = PREFIX
    prepare_parallel.PORTS = PORTS
    prepare_parallel.candidate_module = candidate_module
    if resume:
        prepare_parallel.resume_after_clone(release, payload)
    else:
        prepare_parallel.prepare(release, payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--resume-after-clone", action="store_true")
    args = parser.parse_args()
    try:
        run(args.release, args.payload, resume=args.resume_after_clone)
    except BaseException as error:
        if BASE.exists():
            name = "resume-error.json" if args.resume_after_clone else "prepare-error.json"
            (BASE / name).write_text(
                json.dumps({"type": type(error).__name__, "error": str(error), "at": time.time()})
            )
        raise
