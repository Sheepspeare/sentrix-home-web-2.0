"""One committed latency release: stage/verify safely, activate idle 8872 only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UNIT = "sentrix-atm100-photobench.service"
PREVIOUS = Path("/home/realmagic/xy/experiments/atm1013-20261118/chat-trace-8872-7b34a75b")
RUN = "20261120-162112-2026.11.17-atm-sources-addresses-current-resume-93efab"


def prior():
    # Reuse only the previously verified read-only idle and preservation probes.
    spec = importlib.util.spec_from_file_location("prior_read_probes", PREVIOUS / "release_8872.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, indent=2)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest():
    value = json.loads((HERE / "manifest.json").read_text())
    for name, digest in value["files"].items():
        assert sha(HERE / name) == digest, name
    return value


def inspect():
    value = manifest()
    probes = prior()
    activity = probes.idle()
    pid, environment = probes.process(UNIT)
    args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
    app = Path(args[args.index("--app-dir") + 1])
    assert app == PREVIOUS / "apps/photobench/src", app
    for name, digest in value["baseline"].items():
        assert sha(app / name) == digest, f"Unrelated live source change: {name}"
    if value.get("previous_inspection"):
        before = json.loads(Path(value["previous_inspection"]).read_text())
        assert probes.protected_files(environment) == before["protected_files"]
        assert probes.pids() == before["protected_pids"]
    result = {
        "revision": value["revision"],
        "pid": pid,
        "app_dir": str(app),
        "protected_pids": probes.pids(),
        "protected_files": probes.protected_files(environment),
        "environment_sha256": hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
        "activity": activity,
        "files_verified": len(value["files"]),
        "baseline_files_verified": len(value["baseline"]),
        "other_instance_work": probes.api(8874, "active-work")["work"],
    }
    save("inspection.json", result)
    return {
        k: result[k]
        for k in ("revision", "pid", "files_verified", "baseline_files_verified", "other_instance_work")
    }


def accept():
    from photobench.infrastructure.results.split_store import SplitRunReader

    probes = prior()
    _, environment = probes.process(UNIT)
    root = Path(environment["PHOTOBENCH_RESULTS_ROOT"])
    pointer = root / RUN / "split-commit.json"
    before = sha(pointer)
    reader = SplitRunReader(root, root / RUN)
    try:
        overview = reader.overview()
        entry = reader.entry()
        summary = overview["summary"]
        assert summary["agent_phase_completed_count"] == 34
        assert summary["agent_task_latency_sample_count"] == 34
        assert summary["agent_inherited_count"] == 979
        assert abs(summary["agent_throughput_latency_ms"] - 126481.0) < 0.2
        assert abs(summary["agent_task_latency_mean_ms"] - 425496.6) < 0.2
        assert entry["summary"]["agent_throughput_latency_ms"] == summary["agent_throughput_latency_ms"]
        assert summary["judge_valid_count"] == 1012
        result = {
            "run_id": RUN,
            "agent_metrics": {
                k: v for k, v in summary.items() if k.startswith("agent_") and k != "agent2_trace"
            },
            "judge_valid_count": summary["judge_valid_count"],
            "evaluation_summary": summary["evaluation_summary"],
            "source_manifest_sha256": before,
        }
    finally:
        reader.reader.close()
    assert sha(pointer) == before
    save("acceptance.json", result)
    return result


def activate():
    value = manifest()
    assert (HERE / "acceptance.json").is_file()
    assert not (HERE / "activation-intent.json").exists(), "Already consumed"
    previous = json.loads((HERE / "inspection.json").read_text())
    probes = prior()
    probes.idle()
    pid, environment = probes.process(UNIT)
    assert pid == previous["pid"]
    assert probes.pids() == previous["protected_pids"]
    assert probes.protected_files(environment) == previous["protected_files"]
    assert (
        hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
        == previous["environment_sha256"]
    )
    args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
    args[args.index("--app-dir") + 1] = str(HERE / "apps/photobench/src")
    dropin = Path.home() / ".config/systemd/user" / (UNIT + ".d") / ("z" * 25 + "-agent-latency.conf")
    assert not dropin.exists()
    save("activation-intent.json", {"revision": value["revision"], "old_pid": pid, "dropin": str(dropin)})
    envfile = HERE / "release.env"
    envfile.write_text(
        f"PHOTOBENCH_BUILD_REVISION={value['revision']}+100-agent-latency\nPHOTOBENCH_WEB_DIST_ROOT={HERE}/web-dist\n"
    )
    dropin.write_text(
        "[Service]\nExecStart=\nExecStart=" + shlex.join(args) + f"\nEnvironmentFile={envfile}\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    for attempt in range(45):
        try:
            probes.api(8872, "active-work")
            break
        except OSError:
            if attempt == 44:
                raise
            time.sleep(1)
    return verify()


def verify():
    value = manifest()
    previous = json.loads((HERE / "inspection.json").read_text())
    probes = prior()
    pid, environment = probes.process(UNIT)
    assert pid != previous["pid"]
    assert environment["PHOTOBENCH_BUILD_REVISION"] == value["revision"] + "+100-agent-latency"
    assert environment["PHOTOBENCH_WEB_DIST_ROOT"] == str(HERE / "web-dist")
    assert str(HERE / "apps/photobench/src") in Path(f"/proc/{pid}/cmdline").read_bytes().decode()
    assert probes.pids() == previous["protected_pids"]
    assert probes.protected_files(environment) == previous["protected_files"]
    snapshot = probes.idle()
    overview = probes.api(8872, f"runs/{RUN}?include_history=false")
    expected = json.loads((HERE / "acceptance.json").read_text())
    for key in (
        "agent_throughput_latency_ms",
        "agent_task_latency_mean_ms",
        "agent_phase_completed_count",
        "agent_inherited_count",
    ):
        assert overview["summary"][key] == expected["agent_metrics"][key], key
    assert overview["summary"]["evaluation_summary"] == expected["evaluation_summary"]
    for name, digest in value["files"].items():
        if name.startswith("web-dist/"):
            with urllib.request.urlopen(
                "http://127.0.0.1:8872/" + name.removeprefix("web-dist/"), timeout=30
            ) as response:
                assert hashlib.sha256(response.read()).hexdigest() == digest, name
    result = {
        "revision": value["revision"],
        "pid": pid,
        "restarted_ports": [8872],
        "protected_pids_unchanged": True,
        "protected_files_unchanged": True,
        "actual_idle": snapshot["actual_idle"],
        "agent_metrics": expected["agent_metrics"],
        "judge_valid_count": overview["summary"]["judge_valid_count"],
        "other_instance_work": probes.api(8874, "active-work")["work"],
    }
    save("verification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("inspect", "accept", "activate", "verify"))
    operation = parser.parse_args().operation
    print(json.dumps(globals()[operation](), ensure_ascii=False), flush=True)
