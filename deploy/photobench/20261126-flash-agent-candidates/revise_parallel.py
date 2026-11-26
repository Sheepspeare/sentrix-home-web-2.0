"""Revise only the paused independent instance, preserving its sixteen saved answers."""

import argparse
import json
from pathlib import Path

import prepare_parallel
import revise_limited

STAGE = "parallel-v8"
PREVIOUS_RUN = "20261126-123722-2026.11.17-atm-sources-addresses-big_model-fcc132"
STAGES = {
    STAGE: {
        "runs": {"large": PREVIOUS_RUN},
        "previous_revision": "4c823eacdf09c105cbdcbceb00a182ed2eb50b08",
        "previous_release": "release",
        "accepted": "v7",
        "answer_count": 16,
        "answer_word": "sixteen",
    },
    "parallel-v9": {
        "runs": {"large": "20261126-131611-2026.11.17-atm-sources-addresses-big_model-0e7e57"},
        "previous_revision": "c4bd7f72b75d61b08cf9afaa79bc242c95c90f82",
        "previous_release": "release-c4bd7f72",
        "accepted": "v8",
        "answer_count": 16,
        "answer_word": "sixteen",
    },
    "parallel-v10": {
        "runs": {"large": "20261126-150016-2026.11.17-atm-sources-addresses-big_model-aa3735"},
        "previous_revision": "5c45b791b348a468eba8ec4c5194f86a1145a537",
        "previous_release": "release-5c45b791",
        "accepted": "v9",
        "protection_file": "large-v10-protected.json",
        "answer_count": 16,
        "answer_word": "sixteen",
    },
    "parallel-v11": {
        "runs": {"large": "20261126-165403-2026.11.17-atm-sources-addresses-big_model-d11fd6"},
        "previous_revision": "f1dd6070c3e586cf5e932678286de2bf0f618c0e",
        "previous_release": "release-f1dd6070",
        "accepted": "v10",
        "protection_file": "large-v10-protected.json",
        "answer_count": 16,
        "answer_word": "sixteen",
    },
    "parallel-v12": {
        "runs": {"large": "20261126-180054-2026.11.17-atm-sources-addresses-big_model-5c2604"},
        "previous_revision": "9dbf3ffc3e778bac7d43128d75a0fdfafcdc7678",
        "previous_release": "release-9dbf3ffc",
        "accepted": "v11",
        "protection_file": "large-v10-protected.json",
        "answer_count": 16,
        "answer_word": "sixteen",
    },
}


def configure(stage=STAGE):
    previous = STAGES[stage]
    op = revise_limited
    op.BASE = prepare_parallel.BASE
    op.candidate.BASE = op.BASE
    op.candidate.PREFIX = prepare_parallel.PREFIX
    op.candidate.GROUPS = {"large": prepare_parallel.PORTS}
    op.h.BASE = op.BASE
    op.STAGES[stage] = previous

    def protected():
        protection_file = previous.get("protection_file", "intent.json")
        values = json.loads((op.BASE / protection_file).read_text())["protected"]
        assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == tick for pid, tick in values.items())
        return values

    op.candidate.protected_processes = protected
    return op


def run(revision, product_source, *, stage=STAGE):
    op = configure(stage)
    target = op.BASE / f"release-{revision[:8]}"
    prepare_parallel.compose_release(
        product_source, op.candidate.BASELINE / "release/apps/photobench", target
    )
    op.run(revision, group="large", stage=stage)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--product-source", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, default=STAGE)
    args = parser.parse_args()
    run(args.revision, args.product_source, stage=args.stage)
