#!/usr/bin/env python3
"""Observe the existing build/oracle progress files and wake once on trouble."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted", "finished"}


def inspect(kind, data):
    if kind == "oracle":
        failures = data.get("failed", data.get("failed_primary", 0))
        auxiliary = data.get("numeric_auxiliary_failed", 0)
        return data.get("finished", 0), bool(failures or auxiliary), (
            f"{failures} oracle primary failures, {auxiliary} numeric auxiliary failures"
        )
    phases = data.get("phases", {})
    progress = tuple(
        (name, phase.get("status"), phase.get("progress", {}).get("processed", 0))
        for name, phase in sorted(phases.items()) if name != "gpu_metrics"
    )
    errors = [name for name, phase in phases.items()
              if phase.get("status") == "failed" or phase.get("progress", {}).get("failed", 0)]
    return progress, bool(errors), f"Build failed phases/items: {errors}"


def finalization_progress(progress, detail):
    """Count persisted event updates and completed model calls, not probe time."""
    return progress + ((
        'finalization', detail['events']['revision_sum'],
        detail['model_requests']['successful'],
    ),)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--wait-id", required=True)
    p.add_argument("--build", type=Path, required=True)
    p.add_argument("--oracle", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--build-probe-host")
    p.add_argument("--build-probe-script")
    args = p.parse_args()
    if bool(args.build_probe_host) != bool(args.build_probe_script):
        p.error('Provide both build probe host and script')
    wake = Path.home() / ".codex/skills/wakecodex/scripts/wakecodex.py"
    observations = {}
    deadline = time.monotonic() + 72 * 3600
    args.audit.mkdir(parents=True, exist_ok=True)
    while time.monotonic() < deadline:
        reasons, snapshot = [], {}
        for kind, path in [("build", args.build), ("oracle", args.oracle)]:
            previous = observations.get(kind, {})
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                # Build's existing writer is not atomic; retry on the next tick.
                failed_since = previous.setdefault("unreadable_since", time.monotonic())
                observations[kind] = previous
                if time.monotonic() - failed_since >= 600:
                    reasons.append(f"{kind}: progress unreadable for 10 minutes")
                continue
            previous.pop("unreadable_since", None)
            progress, failed, detail = inspect(kind, data)
            terminal = data.get("status") in TERMINAL
            finalization = None
            stage = data.get('phases', {}).get('pipeline_processing', {}).get('progress', {}).get('stage')
            if kind == 'build' and not terminal and stage == 'finalizing' and args.build_probe_host:
                try:
                    result = subprocess.run(
                        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                         args.build_probe_host, 'python3 ' + args.build_probe_script],
                        check=True, capture_output=True, text=True, timeout=30,
                    )
                    finalization = json.loads(result.stdout)
                    progress = finalization_progress(progress, finalization)
                    previous.pop('probe_unreadable_since', None)
                    error_count = sum(finalization['model_requests']['errors'].values())
                    previous_errors = previous.get('model_errors', error_count)
                    if error_count > previous_errors:
                        reasons.append(f'build: {error_count - previous_errors} new model request failures')
                    previous['model_errors'] = error_count
                except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                    failed_since = previous.setdefault('probe_unreadable_since', time.monotonic())
                    observations[kind] = previous
                    if time.monotonic() - failed_since >= 600:
                        reasons.append('build: finalization probe unreadable for 10 minutes')
                    continue
            changed = time.monotonic() if progress != previous.get("progress") else previous["changed"]
            observations[kind] = {**previous, "progress": progress, "changed": changed, "terminal": terminal}
            snapshot[kind] = {"status": data.get("status"), "progress": progress,
                              "source": str(path), "source_mtime": path.stat().st_mtime,
                              "no_progress_seconds": round(time.monotonic() - changed)}
            if finalization is not None:
                snapshot[kind]['finalization'] = finalization
            if failed:
                reasons.append(f"{kind}: {detail}")
            if not terminal:
                if time.time() - path.stat().st_mtime >= 600:
                    reasons.append(f"{kind}: progress file not updated for 10 minutes")
                if time.monotonic() - changed >= 1800:
                    reasons.append(f"{kind}: no completed-item/phase progress for 30 minutes")
        evidence = {"observed_at": time.time(), "tasks": snapshot, "reasons": reasons}
        path = args.audit / "health-latest.json"
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        if reasons:
            event = {"status": "needs_review", "summary": "; ".join(reasons),
                     "data": {"evidence": str(path)}}
            break
        if all(observations.get(k, {}).get("terminal") for k in ["build", "oracle"]):
            # Each task already has its own completion callback; retire this wait.
            result = subprocess.run(
                [sys.executable, str(wake), "--state", str(args.state), "cancel", args.wait_id],
                capture_output=True, text=True,
            )
            (args.audit / "retired.json").write_text(json.dumps({
                "reason": "Both jobs terminal; existing task callbacks own final review",
                "returncode": result.returncode,
            }))
            return result.returncode
        time.sleep(60)
    else:
        event = {"status": "needs_review", "summary": "Health watcher reached 72-hour deadline"}
    path = args.audit / "health-event.json"
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2))
    result = subprocess.run(
        [sys.executable, str(wake), "--state", str(args.state), "emit", args.wait_id, "--file", str(path)],
        capture_output=True, text=True,
    )
    (args.audit / "health-emission.txt").write_text(result.stdout + result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
