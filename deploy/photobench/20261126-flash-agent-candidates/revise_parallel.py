"""Revise only the paused independent instance, preserving its sixteen saved answers."""

import argparse
import json
from pathlib import Path

import prepare_parallel
import revise_limited

STAGE = "parallel-v8"
PREVIOUS_RUN = "20261126-123722-2026.11.17-atm-sources-addresses-big_model-fcc132"


def configure():
    op = revise_limited
    op.BASE = prepare_parallel.BASE
    op.candidate.BASE = op.BASE
    op.candidate.PREFIX = prepare_parallel.PREFIX
    op.candidate.GROUPS = {"large": prepare_parallel.PORTS}
    op.h.BASE = op.BASE
    op.STAGES[STAGE] = {
        "runs": {"large": PREVIOUS_RUN},
        "previous_revision": "4c823eacdf09c105cbdcbceb00a182ed2eb50b08",
        "previous_release": "release",
        "accepted": "v7",
        "answer_count": 16,
        "answer_word": "sixteen",
    }

    def protected():
        values = json.loads((op.BASE / "intent.json").read_text())["protected"]
        assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in values.items())
        return values

    op.candidate.protected_processes = protected
    return op


def run(revision, product_source):
    op = configure()
    target = op.BASE / f"release-{revision[:8]}"
    prepare_parallel.compose_release(
        product_source, op.candidate.BASELINE / "release/apps/photobench", target
    )
    op.run(revision, group="large", stage=STAGE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--product-source", type=Path, required=True)
    args = parser.parse_args()
    run(args.revision, args.product_source)
