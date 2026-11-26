"""Launch the independent v7 instance once, then watch its first 16 answers."""

import argparse
import json
from pathlib import Path

import prepare_parallel
import watch_candidates


def run(audit):
    watch_candidates.BASE = str(prepare_parallel.BASE)
    watch_candidates.PORTS = {"large": prepare_parallel.PORTS[1]}
    watch_candidates.PRODUCT_PORTS = {"large": prepare_parallel.PORTS[0]}
    prepared = json.loads(watch_candidates.ssh(f"cat {prepare_parallel.BASE}/prepared.json"))
    payload = json.loads((audit / "large-run-payload.json").read_text())
    assert prepared["memory_18_tables_unchanged"]
    assert payload["qa_concurrency_control_path"] == str(
        prepare_parallel.BASE / "large/dispatch-control.json"
    )
    assert watch_candidates.api(prepare_parallel.PORTS[1], "active-work")["work"] == []
    return watch_candidates.run("large", audit, audit / "first-sixteen", target=16)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.audit))
