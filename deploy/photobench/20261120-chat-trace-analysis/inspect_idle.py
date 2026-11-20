"""Read all product activity before reviewing an evaluator-only deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from inspect_candidate import UNITS, api, process


def service(unit):
    pid, environment = process(unit)
    args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
    return {
        "pid": pid,
        "app_dir": args[args.index("--app-dir") + 1],
        "cwd": str(Path(f"/proc/{pid}/cwd").resolve()),
        "configuration": {
            key: value
            for key, value in environment.items()
            if key
            in {
                "PHOTOBENCH_BUILD_REVISION",
                "PHOTOBENCH_WEB_DIST_ROOT",
                "SENTRIX_DATA_DIR",
                "SENTRIX_REASONING_MAX_MODEL_CALLS",
                "SENTRIX_REASONING_MAX_TOOL_CALLS",
                "SENTRIX_REASONING_MAX_SEARCH_CALLS",
                "SENTRIX_REASONING_MAX_OTHER_TOOL_CALLS",
            }
        },
        "unit_paths": subprocess.check_output(
            ["systemctl", "--user", "show", unit, "-p", "EnvironmentFiles", "-p", "DropInPaths"],
            text=True,
        ).splitlines(),
        "memory": {key: status[key].strip() for key in ("VmRSS", "VmHWM")},
    }, environment


def inspect(port, candidate=None):
    evaluator, product, product_port = UNITS[port]
    evaluator_info, _ = service(evaluator)
    product_info, environment = service(product)
    work = api(port, "active-work")["work"]
    progress = api(product_port, "imports/progress")
    database = Path(environment["SENTRIX_DATA_DIR"]) / "sentrix.db"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as db:
        db.row_factory = sqlite3.Row
        turns = [
            dict(row)
            for row in db.execute(
                "select id,scope_id,status,cancel_requested,created_at,updated_at "
                "from agent_turns where status in ('pending','running')"
            )
        ]
        pipelines = [
            dict(row)
            for row in db.execute(
                "select operation_id,scope_id,status,started_at from pipeline_receipts where status='running'"
            )
        ]
        jobs = {
            table: [
                dict(row)
                for row in db.execute(
                    f"select status,count(*) as count from {table} "
                    "where status in ('pending','running','processing','queued') group by status"
                )
            ]
            for table in ("import_jobs", "video_jobs")
        }
        postprocess = db.execute(
            "select count(*) from scope_postprocess where processing_generation is not null"
        ).fetchone()[0]
    services = {evaluator: evaluator_info, product: product_info}
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    result = {
        "port": port,
        "service_pids": {unit: info["pid"] for unit, info in services.items()},
        "services": services,
        "work": work,
        "imports": progress,
        "live_turn_count": len(turns),
        "product_activity": {
            "turns": turns,
            "pipelines": pipelines,
            "jobs": jobs,
            "postprocess_count": postprocess,
        },
        "idle": not work
        and not turns
        and not pipelines
        and not any(jobs.values())
        and not postprocess
        and progress["library"]["processing"] == 0
        and progress["postprocess"]["state"] == "idle",
        "disk_free": {str(p): shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "memory_available_kib": int(memory["MemAvailable"].split()[0]),
    }
    if candidate:
        manifest = json.loads((candidate / "candidate-manifest.json").read_text())
        mismatches = [
            name
            for name, expected in manifest["files"].items()
            if hashlib.sha256((candidate / name).read_bytes()).hexdigest() != expected
        ]
        result["candidate"] = {
            "path": str(candidate),
            "revision": manifest["revision"],
            "files_checked": len(manifest["files"]),
            "mismatches": mismatches,
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--idle-port", type=int, choices=list(UNITS), required=True)
    parser.add_argument("--candidate-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.idle_port, args.candidate_dir)), flush=True)
