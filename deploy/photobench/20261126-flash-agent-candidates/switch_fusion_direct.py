"""Drain the fusion run, then resume its saved answers with direct cloud I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-condition-fusion-20261126")
REVISION = "bc73615327e62f5dcd60ae018a61aae883759ad8"
OLD_SUITES_SHA = "fb0e13e6c92caac6f875d08ae184327055304c8f791fe6f5239c9f2b9ed045f6"
PYTHON = "/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python"
UNITS = [
    "sentrix-flash-condition-fusion-large-api.service",
    "sentrix-flash-condition-fusion-large-eval.service",
]


def save(path, value):
    part = path.with_suffix(path.suffix + ".part")
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    part.chmod(0o600)
    part.replace(path)


def api(port, path, body=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_ticks():
    units = subprocess.check_output(
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain", "sentrix*.service"],
        text=True,
    )
    result = {}
    for line in units.splitlines():
        name = next((part for part in line.split() if part.endswith(".service")), "")
        if not name or name in UNITS:
            continue
        pid = subprocess.check_output(
            ["systemctl", "--user", "show", name, "-p", "MainPID", "--value"], text=True
        ).strip()
        if int(pid) > 0:
            result[pid] = Path(f"/proc/{pid}/stat").read_text().split()[21]
    return result


def probe():
    command = BASE / "ops-storage-9dff4905/relocate_fusion_database.py"
    return json.loads(subprocess.check_output([PYTHON, str(command), "--probe"], text=True))


def answer_hashes(run):
    hashes, page = {}, 1
    while True:
        listing = api(8883, f"runs/{run}/items?page={page}&page_size=100")
        for row in listing["items"]:
            item = api(8883, f"runs/{run}/items/{row['index']}")["item"]
            assert item["qa_id"] == row["qa_id"]
            rev = item["trace_storage"]["revision"]
            parts, offset = [], 0
            while True:
                chunk = api(
                    8883,
                    f"runs/{run}/items/{row['index']}/content?path=answer&offset={offset}&limit=65536&revision={rev}",
                )
                assert chunk["read_revision"] == rev
                parts.append(chunk["text"])
                offset = chunk.get("next_offset")
                if offset is None:
                    break
            hashes[row["qa_id"]] = hashlib.sha256("".join(parts).encode()).hexdigest()
        if not listing["has_next"]:
            return hashes
        page += 1


def dropin(unit, values, *, evaluation_source=None):
    directory = Path.home() / ".config/systemd/user" / (unit + ".d")
    directory.mkdir(exist_ok=True)
    text = "[Service]\n"
    text += "EnvironmentFile=" + str(BASE.with_name("qwen38-flash-20261124") / "secrets.env") + "\n"
    text += "".join("Environment=" + json.dumps(f"{key}={value}") + "\n" for key, value in values.items())
    if evaluation_source is not None:
        text += (
            f"ExecStart=\nExecStart={PYTHON} -m uvicorn --factory photobench.main:create_app "
            f"--host 0.0.0.0 --port 8883 --app-dir {evaluation_source}/src\n"
        )
    (directory / "20-direct-cloud16.conf").write_text(text)


def main(run, package):
    output = BASE / "direct-cloud16"
    output.mkdir()
    protected = process_ticks()
    save(
        output / "intent.json",
        {
            "at": time.time(),
            "source_run": run,
            "revision": REVISION,
            "workers": 16,
            "protected": protected,
            "preserve_inflight": True,
        },
    )
    deadline = time.monotonic() + 7500
    while time.monotonic() < deadline:
        live, worker = api(8883, f"runs/{run}?include_history=false"), probe()
        progress = live["phases"]["qa_eval"]["progress"]
        assert live["build_revision"] == REVISION and not live.get("fatal_error")
        assert progress["dispatch_paused"]
        save(output / "drain-progress.json", {"at": time.time(), "progress": progress, "worker": worker})
        if not any(worker["turn_statuses"].get(k, 0) for k in ["running", "pending"]):
            break
        time.sleep(20)
    else:
        raise TimeoutError("In-flight turns still active; keep original run and inspect")
    original = answer_hashes(run)
    assert len(original) == progress["agent_completed"] >= 20
    save(
        output / "drained.json",
        {"at": time.time(), "progress": progress, "answer_sha256": original, "worker": worker},
    )
    save(
        output / "cancel-intent.json",
        {"run_id": run, "active_turns": 0, "purpose": "cancel unstarted dispatch only; resume saved answers"},
    )
    save(output / "cancel-response.json", api(8883, f"runs/{run}/cancel", {}))
    for _ in range(90):
        old = api(8883, f"runs/{run}?include_history=false")
        if (
            old["status"] in {"cancelled", "interrupted", "completed_with_errors"}
            and not api(8883, "active-work")["work"]
        ):
            break
        time.sleep(1)
    else:
        raise TimeoutError("Inspect original cancellation before any restart")
    assert answer_hashes(run) == original
    live_release = BASE / "release-bc736153"
    suites = live_release / "apps/photobench/src/photobench/application/runs/suites.py"
    assert sha(suites) == OLD_SUITES_SHA
    shutil.copytree(
        (live_release / "apps/photobench").resolve(),
        output / "evaluation",
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mypy_cache"),
    )
    shutil.copy2(package / "suites.py", output / "evaluation/src/photobench/application/runs/suites.py")
    shutil.copytree(BASE / "large/config", output / "config")
    shutil.copytree(package / "transport", output / "transport")
    cloud = json.loads((BASE.with_name("qwen38-flash-20261124") / "gateway-config.json").read_text())[
        "url"
    ].rstrip("/")
    assert cloud.startswith("https://")
    runtime = json.loads((output / "config/runtime_connection.json").read_text())
    runtime["model_base_url"] = cloud
    runtime["product_settings"] = {**old["runtime_snapshot"]["product_settings"], "agent_turn_workers": 16}
    save(output / "config/runtime_connection.json", runtime)
    profile = json.loads((BASE / "model-profile.json").read_text())
    for role in profile["roles"].values():
        assert role["model"] == "qwen3.8-flash" and role["max_output_tokens"] == 4096
        role["base_url"] = cloud
    profile.setdefault("agent", {})["turn_workers"] = 16
    save(output / "model-profile.json", profile)
    save(
        output / "configured.json",
        {
            "at": time.time(),
            "cloud_url": cloud,
            "model_profile": profile,
            "runtime": runtime,
            "suites_sha256": sha(package / "suites.py"),
            "product_business_revision": REVISION,
        },
    )
    subprocess.run(["systemctl", "--user", "stop", *reversed(UNITS)], check=True)
    for role, unit in zip(["qa", "eval"], UNITS, strict=True):
        values = {
            "PYTHONPATH": output / "transport",
            "FLASH_DIRECT_URL": cloud,
            "FLASH_DIRECT_HTTP_DIR": output / role / "http/raw",
            "PHOTOBENCH_CONFIG_ROOT": output / "config",
        }
        if role == "qa":
            values.update(
                SENTRIX_LLM_CONFIG=output / "model-profile.json",
                SENTRIX_AGENT_TURN_MAX_WORKERS=16,
                SENTRIX_VLM_BASE_URL=cloud,
            )
        else:
            values.update(BENCH_VLLM_BASE_URL=cloud, BENCH_BIG_MODEL_BASE_URL=cloud)
        dropin(unit, values, evaluation_source=output / "evaluation" if role == "eval" else None)
    save(BASE / "large/dispatch-control.json", {"concurrency": 16, "pause_after_completed": 1013})
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", *UNITS], check=True)
    for _ in range(180):
        try:
            health = api(8303, "health/live")
            config = api(8883, "config")
            if health["build_revision"] == REVISION and config["runtime_config"]["model_base_url"] == cloud:
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        raise TimeoutError("Read service logs; never replay completed preparation")
    diagnostics = api(8303, "diagnostics")
    effective = diagnostics["runtime_configuration"]
    assert (
        effective["agent_turn_workers"] == 16
        and effective["reasoning_max_model_calls"] == 100
        and effective["reasoning_max_tool_calls"] == 600
    )
    assert effective["vlm_base_url"] == cloud
    assert all(Path(f"/proc/{pid}/stat").read_text().split()[21] == ticks for pid, ticks in protected.items())
    save(
        output / "verification.json",
        {
            "at": time.time(),
            "effective": effective,
            "protected_pids_unchanged": True,
            "old_answer_sha256": original,
            "direct_cloud": True,
            "album_rebuilt": False,
        },
    )
    payload = {"qa_concurrency": 16, "model_base_url": cloud, "repair_missing_judges": False}
    save(output / "resume-intent.json", {"at": time.time(), "source_run": run, "payload": payload})
    reply = api(8883, f"runs/{run}/resume", payload)
    save(output / "resume-response.json", reply)
    new = reply["run_id"]
    for _ in range(180):
        resumed = api(8883, f"runs/{new}?include_history=false")
        if resumed.get("fatal_error"):
            raise RuntimeError(resumed["fatal_error"])
        current = resumed.get("phases", {}).get("qa_eval", {}).get("progress", {})
        worker = probe()
        if current.get("qa_concurrency") == 16 and worker["turn_statuses"].get("running", 0) == 16:
            break
        time.sleep(1)
    else:
        raise TimeoutError("Resume already submitted; inspect actual worker before any retry")
    inherited = answer_hashes(new)
    assert all(inherited[q] == answer for q, answer in original.items())
    assert resumed["qa_snapshot"] == old["qa_snapshot"] and resumed["build_revision"] == REVISION
    save(
        output / "started.json",
        {
            "at": time.time(),
            "run_id": new,
            "source_run": run,
            "qa_concurrency": 16,
            "inherited_answer_count": len(original),
            "inherited_answer_sha256_verified": True,
            "worker": worker,
            "progress": current,
            "direct_cloud": True,
            "revision": REVISION,
        },
    )
    save(
        output / "terminal.json",
        {
            "status": "completed",
            "at": time.time(),
            "run_id": new,
            "source_run": run,
            "stage": "direct16_started",
            "full_qa_completed": False,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    try:
        main(args.run, args.package)
    except Exception as error:
        folder = BASE / "direct-cloud16"
        if folder.exists():
            save(
                folder / "terminal.json",
                {
                    "status": "failed",
                    "at": time.time(),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "recovery": "Read stage receipts, keep successes; do not replay cancel/resume",
                },
            )
        raise
