"""Start the independent fusion run once and observe its sixteen-answer checkpoint."""

import argparse
import json
from pathlib import Path

import prepare_fusion
import watch_candidates


def run(audit):
    watch_candidates.BASE = str(prepare_fusion.BASE)
    watch_candidates.PORTS = {"large": prepare_fusion.PORTS[1]}
    watch_candidates.PRODUCT_PORTS = {"large": prepare_fusion.PORTS[0]}
    prepared = json.loads(watch_candidates.ssh(f"cat {prepare_fusion.BASE}/prepared.json"))
    assert prepared["memory_18_tables_unchanged"]
    assert watch_candidates.api(prepare_fusion.PORTS[1], "active-work")["work"] == []
    return watch_candidates.run("large", audit, audit / "first-sixteen", target=16)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.audit))
