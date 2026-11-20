"""Review and activate one committed evaluator candidate on 8872 only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROBE = Path("/home/realmagic/xy/experiments/atm1013-20261118/chat-trace-idle-review-9c3c783")
sys.path.insert(0, str(PROBE))
from inspect_candidate import api, process  # noqa: E402
from inspect_idle import inspect  # noqa: E402

HERE = Path(__file__).resolve().parent
UNIT = "sentrix-atm100-photobench.service"
PRODUCT = "sentrix-atm100-api.service"
OLD_APP = "/home/realmagic/xy/experiments/atm1013-20261118/psh-formal-f75c105/evaluation/apps/photobench/src"
ARCHIVED = {
    "turn_20261110064059251617",
    "turn_20261110064059252188",
    "turn_20261110064059253059",
    "turn_20261110064059253624",
    "turn_20261110065608379748",
    "turn_20261110065608411216",
    "turn_20261110065608797731",
    "turn_20261110065608846083",
    "turn_20261110071117717857",
    "turn_20261110071117916274",
}
PIPELINE = "47886b67748349c3b98427ac51ab539c"
PROTECTED = [
    PRODUCT,
    "sentrix-atm100-model.service",
    "sentrix-atm100-wemm.service",
    "sentrix-atm100-doubao-api.service",
    "sentrix-atm100-doubao-photobench.service",
    "sentrix-atm100-qwen37-gateway.service",
    "sentrix-atm100-doubao-gateway.service",
    "sentrix2-api.service",
    "sentrix2-photobench.service",
]


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, indent=2)


def pids():
    return {unit: process(unit)[0] for unit in PROTECTED}


def started(pid):
    boot = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    return datetime.fromtimestamp(boot + ticks / os.sysconf("SC_CLK_TCK"), UTC)


def idle():
    value = inspect(8872)
    assert not value["work"], value["work"]
    activity = value["product_activity"]
    product = value["services"][PRODUCT]
    product_start = started(product["pid"])
    turns = activity["turns"]
    assert {turn["id"] for turn in turns} == ARCHIVED
    assert all(
        turn["status"] == "pending"
        and turn["cancel_requested"] == 1
        and datetime.fromisoformat(turn["updated_at"]) < product_start
        for turn in turns
    )
    source = Path(product["app_dir"]) / "sentrix/infrastructure/persistence/repositories/turns.py"
    text = source.read_text()
    exclusion = "id NOT IN (" + ", ".join(repr(key) for key in sorted(ARCHIVED)) + ") AND "
    assert exclusion in text, "Recorded historical turn exclusion changed"
    pipelines = activity["pipelines"]
    assert len(pipelines) == 1 and pipelines[0]["operation_id"] == PIPELINE
    assert datetime.fromisoformat(pipelines[0]["started_at"]) < product_start
    tracker = Path(product["app_dir"]) / "sentrix/application/media/track_pipeline.py"
    assert digest(tracker) == manifest()["pipeline_tracker_sha256"]
    assert "self._active_scopes: set[str] = set()" in tracker.read_text()
    assert not any(activity["jobs"].values()) and activity["postprocess_count"] == 0
    assert value["imports"]["library"]["processing"] == 0
    assert value["imports"]["postprocess"]["state"] == "idle"
    value.update(
        actual_idle=True,
        historical_records_preserved=True,
        product_started_at=product_start.isoformat(),
        turn_exclusion_sha256=digest(source),
        pipeline_owner_predates_process=True,
    )
    return value


def manifest():
    return json.loads((HERE / "candidate-manifest.json").read_text())


def candidate():
    value = manifest()
    for name, expected in value["files"].items():
        assert digest(HERE / name) == expected, name
    return value


def protected_files(environment):
    config = Path(environment["PHOTOBENCH_CONFIG_ROOT"])
    results = Path(environment["PHOTOBENCH_RESULTS_ROOT"])
    files = [p for p in config.rglob("*") if p.is_file()]
    files += list(results.glob("*/split-commit.json"))
    files += list(results.glob("*/snapshot-commit.json"))
    chat = results.with_name(results.name + "-chat")
    files += list(chat.glob("*.json"))
    return {str(p): digest(p) for p in files}


def inspect_release():
    value = candidate()
    activity = idle()
    pid, environment = process(UNIT)
    assert activity["services"][UNIT]["app_dir"] == OLD_APP
    result = {
        "candidate": value["revision"],
        "files_verified": len(value["files"]),
        "activity": activity,
        "protected_pids": pids(),
        "protected_files": protected_files(environment),
        "old_pid": pid,
        "environment_digest": hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
    }
    save("inspection.json", result)
    return {k: result[k] for k in ("candidate", "files_verified", "old_pid")}


def activate():
    assert not (HERE / "activate-intent.json").exists(), "Activation already consumed"
    value = candidate()
    previous = json.loads((HERE / "inspection.json").read_text())
    snapshot = idle()
    pid, environment = process(UNIT)
    assert pid == previous["old_pid"] and pids() == previous["protected_pids"]
    assert protected_files(environment) == previous["protected_files"]
    assert (
        hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
        == previous["environment_digest"]
    )
    args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
    index = args.index("--app-dir") + 1
    assert args[index] == OLD_APP
    args[index] = str(HERE / "apps/photobench/src")
    dropin = Path.home() / ".config/systemd/user" / (UNIT + ".d") / ("z" * 24 + "-chat-trace-8872.conf")
    assert not dropin.exists()
    save(
        "activate-intent.json",
        {
            "old_pid": pid,
            "protected_pids": previous["protected_pids"],
            "candidate": value["revision"],
            "snapshot": snapshot,
            "new_dropin": str(dropin),
        },
    )
    envfile = HERE / "release.env"
    envfile.write_text(
        f"PHOTOBENCH_BUILD_REVISION={value['revision']}+100-chat-trace\n"
        f"PHOTOBENCH_WEB_DIST_ROOT={HERE}/web-dist\n"
    )
    dropin.write_text(
        "[Service]\nExecStart=\nExecStart=" + shlex.join(args) + f"\nEnvironmentFile={envfile}\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    for attempt in range(60):
        try:
            api(8872, "active-work")
            break
        except OSError:
            if attempt == 59:
                raise
            time.sleep(1)
    current, actual = process(UNIT)
    assert current != pid
    assert actual["PHOTOBENCH_BUILD_REVISION"] == value["revision"] + "+100-chat-trace"
    assert actual["PHOTOBENCH_WEB_DIST_ROOT"] == str(HERE / "web-dist")
    expected = {
        **environment,
        "PHOTOBENCH_BUILD_REVISION": actual["PHOTOBENCH_BUILD_REVISION"],
        "PHOTOBENCH_WEB_DIST_ROOT": actual["PHOTOBENCH_WEB_DIST_ROOT"],
    }
    # systemd assigns a fresh invocation/journal identity to every restarted process.
    volatile = {"INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID"}
    assert {k: v for k, v in actual.items() if k not in volatile} == {
        k: v for k, v in expected.items() if k not in volatile
    }
    assert pids() == previous["protected_pids"]
    assert protected_files(actual) == previous["protected_files"]
    after = idle()
    response = {
        "revision": value["revision"],
        "old_pid": pid,
        "pid": current,
        "app_dir": str(HERE / "apps/photobench/src"),
        "static_dir": actual["PHOTOBENCH_WEB_DIST_ROOT"],
        "protected_pids_unchanged": True,
        "protected_files_unchanged": True,
        "configuration_preserved": True,
        "historical_records_preserved": True,
        "actual_idle": after["actual_idle"],
        "restarted_ports": [8872],
    }
    save("activate-response.json", response)
    return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("inspect", "activate"))
    args = parser.parse_args()
    print(json.dumps(inspect_release() if args.operation == "inspect" else activate()), flush=True)
