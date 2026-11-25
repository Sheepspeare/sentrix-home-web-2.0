"""Start each candidate once and wait for its eight-question review checkpoint."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

BASE = "/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-agent-candidates-20261126"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "192.168.0.100"]
GATEWAY_LOG = (
    "/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-dual-baseline-20261126/model-requests.jsonl"
)
PORTS = {"limited": 8878, "large": 8879}
PRODUCT_PORTS = {"limited": 8298, "large": 8299}
CHECKPOINT = 8
TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "interrupted"}


def scrub(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in {"api_key", "judge_api_key", "callback_token", "authorization"}
            else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(scrub(value), ensure_ascii=False, indent=2))
    temporary.replace(path)


def ssh(command):
    return subprocess.run([*SSH, command], capture_output=True, text=True, timeout=60, check=True).stdout


def api(port, path):
    return json.loads(ssh(f"curl --max-time 45 -fsS http://127.0.0.1:{port}/api/v1/{path}"))


def checkpoint(snapshot):
    phase = (snapshot.get("phases") or {}).get("qa_eval") or {}
    progress = phase.get("progress") or {}
    if snapshot.get("status") in TERMINAL:
        return "run_terminal"
    if progress.get("dispatch_paused") and progress.get("agent_completed", 0) >= CHECKPOINT:
        return "first_eight_ready"
    return None


def request_alert(records, started):
    recent = [record for record in records if record.get("started_at", 0) >= started]
    if any(
        record.get("enable_thinking") is not False
        or record.get("actual_model") not in {None, "qwen3.8-flash"}
        for record in recent
    ):
        return "model_configuration_drift"
    cutoff = time.time() - 120
    if sum(r.get("http_status") == 429 and r.get("started_at", 0) >= cutoff for r in recent) >= 3:
        return "repeated_429"
    return None


def validate_payload(group, payload):
    assert payload["sentrix_url"] == f"http://127.0.0.1:{PRODUCT_PORTS[group]}"
    assert payload["qa_concurrency"] == 2
    assert payload["reasoning"] is True and payload["defer_scoring"] is True


def run(group, audit, output=None, *, existing_run=""):
    port = PORTS[group]
    output = output or audit / f"{group}-first-eight"
    output.mkdir()  # Existing intent requires inspection, never another POST.
    started = time.time()
    intent = {
        "group": group,
        "at": started,
        "payload": f"{BASE}/{group}/run-payload.json",
        "existing_run": existing_run or None,
    }
    save(output / "intent.json", intent)
    try:
        payload_file = audit / f"{group}-run-payload.json"
        payload = json.loads(payload_file.read_text())
        validate_payload(group, payload)
        digest = hashlib.sha256(payload_file.read_bytes()).hexdigest()
        assert ssh(f"sha256sum {intent['payload']}").split()[0] == digest
        if existing_run:
            snapshot = api(port, f"runs/{existing_run}?include_history=false")
            assert snapshot["runtime_snapshot"]["sentrix_url"] == payload["sentrix_url"]
            reply = {"run_ids": [existing_run], "mode": "attach_monitor_no_post"}
        else:
            reply = json.loads(
                ssh(
                    f"curl --max-time 45 -fsS -X POST http://127.0.0.1:{port}/api/v1/runs "
                    f"-H 'Content-Type: application/json' --data-binary @{BASE}/{group}/run-payload.json"
                )
            )
        save(output / "launch.json", reply)
        assert len(reply["run_ids"]) == 1
        run_id = reply["run_ids"][0]
        consecutive_errors = 0
        while time.time() - started < 4 * 3600:
            try:
                snapshot = api(port, f"runs/{run_id}?include_history=false")
                save(output / "latest.json", snapshot)
                log_tail = ssh(f"tail -n 500 {GATEWAY_LOG}")
                records = [json.loads(line) for line in log_tail.splitlines()]
                save(output / "request-metadata.json", records)
                free_bytes = int(ssh("df -Pk /home").splitlines()[-1].split()[3]) * 1024
                consecutive_errors = 0
            except (subprocess.SubprocessError, ValueError):
                consecutive_errors += 1
                if consecutive_errors >= 4:
                    raise
                time.sleep(15)
                continue
            reason = request_alert(records, started) or checkpoint(snapshot)
            if free_bytes < 2 * 1024**3:
                reason = "disk_below_2GiB"
            if reason:
                save(output / "items.json", api(port, f"runs/{run_id}/items?page=1&page_size=10"))
                failed = reason != "first_eight_ready" and snapshot.get("status") != "completed"
                terminal = {
                    "status": "needs_review" if not failed else "failed",
                    "reason": reason,
                    "run_id": run_id,
                    "at": time.time(),
                    "free_bytes": free_bytes,
                    "worker_stopped_by_watcher": False,
                }
                save(output / "terminal.json", terminal)
                print(json.dumps(terminal), flush=True)
                return 1 if failed else 0
            time.sleep(15)
        raise TimeoutError("First eight checkpoint deadline; inspect live turns before resuming")
    except Exception as error:
        save(
            output / "terminal.json",
            {"status": "failed", "error": str(error), "at": time.time(), "worker_stopped_by_watcher": False},
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=["limited", "large"], required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--existing-run", default="")
    args = parser.parse_args()
    raise SystemExit(run(args.group, args.audit, args.output, existing_run=args.existing_run))
