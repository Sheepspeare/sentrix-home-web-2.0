"""Resize the experiment gateway after an idle boundary; drain accepted requests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-dual-baseline-20261126")
UNIT = "sentrix-flash-dual-gateway.service"
EXPECTED = "665c9afbd193f7ce7ff4a240e2078b0d1478be13cab591687848a3f06ed8a659"


def main():
    output = BASE / "gateway-resize-24"
    output.mkdir()

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + "\n")

    def health():
        with urllib.request.urlopen("http://127.0.0.1:18148/health", timeout=3) as response:
            return json.load(response)

    def pid():
        return subprocess.check_output(
            ["systemctl", "--user", "show", UNIT, "-p", "MainPID", "--value"], text=True
        ).strip()

    assert hashlib.sha256((BASE / "scripts/gateway.py").read_bytes()).hexdigest() == EXPECTED
    path = BASE / "gateway-config.json"
    config = json.loads(path.read_text())
    assert config["max_concurrency"] == 8
    before = health()
    assert before["max_concurrency"] == 8
    save(
        "intent.json",
        {
            "at": time.time(),
            "before": before,
            "pid": pid(),
            "new_max_concurrency": 24,
            "agent_concurrency_changed": False,
        },
    )
    dropin = Path.home() / ".config/systemd/user" / (UNIT + ".d") / "graceful-drain.conf"
    dropin.parent.mkdir(exist_ok=True)
    assert not dropin.exists()
    dropin.write_text("[Service]\nTimeoutStopSec=infinity\nSendSIGKILL=no\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    deadline = time.monotonic() + 60
    while True:
        state = health()
        if state["inflight_requests"] == 0:
            break
        if time.monotonic() >= deadline:
            save(
                "terminal.json",
                {"status": "needs_review", "reason": "no_idle_boundary", "last_health": state},
            )
            return 1
        time.sleep(0.2)
    save("idle-boundary.json", {"at": time.time(), "health": state})
    config["max_concurrency"] = 24
    temporary = path.with_suffix(".resize.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)
    # Uvicorn SIGTERM finishes accepted requests; systemd cannot kill them on a deadline.
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    for _ in range(100):
        try:
            state = health()
            if state["max_concurrency"] == 24:
                save(
                    "terminal.json",
                    {
                        "status": "completed",
                        "at": time.time(),
                        "health": state,
                        "pid": pid(),
                        "gateway_source_unchanged": True,
                    },
                )
                print(json.dumps(state))
                return 0
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Gateway health did not confirm 24 slots")


if __name__ == "__main__":
    raise SystemExit(main())
