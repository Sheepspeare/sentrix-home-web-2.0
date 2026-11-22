"""Wake once when the four existing product turns finish or need inspection."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "20261118-atm1013-full"))
from monitor_event import emit_event, terminal_event  # noqa: E402


def review(snapshot):
    for field in (
        "service_pids_match",
        "evaluation_app_matches",
        "writer_source_matches",
        "source_manifest_unchanged",
        "original_manifest_unchanged",
        "active_mapping_matches",
        "model_matches",
        "product_limits_match",
        "product_database_match",
    ):
        if not snapshot[field]:
            return "Existing product turn invariant changed: " + field
    if snapshot["reasoning_context_tokens"] != 16384:
        return "Existing product turn context changed"
    if snapshot["active_evaluation"]:
        return "Another evaluator activity appeared while collecting four original turns"
    if min(snapshot["disk_free"].values()) < 1536 * 1024**2:
        return "Disk free space below 1.5 GiB"
    if snapshot["memory_available_kib"] < 4 * 1024**2:
        return "Server available memory below 4 GiB"
    if max(snapshot["process_rss_kib"].values()) > 24 * 1024**2:
        return "A Doubao service RSS exceeded 24 GiB"
    if snapshot["all_terminal"]:
        return "Four original product turns are terminal; collect answers and resume only 19 never-started QA"
    return ""


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--remote-script", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures = 0
    started = changed = time.monotonic()
    prior = None
    while True:
        reason = ""
        try:
            process = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "ServerAliveInterval=15",
                    "-o",
                    "ServerAliveCountMax=2",
                    "192.168.0.100",
                    "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python",
                    args.remote_script,
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            )
            result = json.loads(process.stdout)
            failures = 0
            reason = review(result)
            progress = json.dumps(
                [[t["id"], t["status"], t["event_seq"], t["attempt_no"]] for t in result["turns"]],
                sort_keys=True,
            )
            if progress != prior:
                changed, prior = time.monotonic(), progress
            if not reason and time.monotonic() - changed > 7200:
                reason = "Four original product turns made no recorded progress for two hours"
            if not reason and time.monotonic() - started > 8 * 3600:
                reason = "Existing product turn monitor reached eight-hour review window"
        except Exception as error:
            failures += 1
            result = {
                "stage": "doubao_existing_four_turns",
                "consecutive_failures": failures,
                "probe_error": str(error)[:2000],
            }
            if isinstance(error, subprocess.CalledProcessError):
                result["probe_stderr"] = (error.stderr or "")[-3000:]
            if failures >= 3:
                reason = "Three consecutive four-turn probes failed"
        evidence = args.audit / "turns-latest.json"
        evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if args.check:
            print(
                json.dumps(
                    {"all_terminal": result.get("all_terminal"), "reason": reason, "failures": failures},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 1 if reason or failures else 0
        if reason:
            event = args.audit / ("turns-event-" + str(int(time.time())) + ".json")
            event.write_text(
                json.dumps(terminal_event(reason, result, evidence), ensure_ascii=False, indent=2)
            )
            emit_event(args.registration, event)
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
