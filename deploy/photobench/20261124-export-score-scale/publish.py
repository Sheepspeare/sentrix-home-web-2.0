"""Publish only the export-filter UI; retain old hashed assets and all service processes."""

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = Path("/home/realmagic/xy/benchmarks/atm-bench/experiments/atm1013/releases/main-56ea57aa/web-dist")
UNITS = [
    "sentrix-atm100-photobench",
    "sentrix-atm100-doubao-photobench",
    "sentrix-atm100-api",
    "sentrix-atm100-doubao-api",
    "sentrix2-api",
    "sentrix2-photobench",
    "sentrix-atm100-model",
    "sentrix-atm100-wemm",
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pids():
    return subprocess.check_output(
        ["systemctl", "--user", "show", *[u + ".service" for u in UNITS], "-p", "Id", "-p", "MainPID"],
        text=True,
    )


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/{path}", timeout=30) as response:
        return response.read()


def save(name, value):
    (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def publish():
    manifest = json.loads((HERE / "manifest.json").read_text())
    assert not (HERE / "publish-intent.json").exists(), "Already attempted; inspect receipt first"
    before = pids()
    for port in [8872, 8874]:
        assert json.loads(get(port, "api/v1/config"))["build_revision"] == "56ea57aa+100-main-merge"
        for name, digest in manifest["previous_files"].items():
            assert sha(WEB / name) == digest, ("Server changed", name)
            assert hashlib.sha256(get(port, name)).hexdigest() == digest
    for name, digest in manifest["files"].items():
        assert sha(HERE / "web-dist" / name) == digest
        existing = WEB / name
        if name != "index.html" and existing.exists():
            assert sha(existing) == digest, ("Asset collision", name)
    shutil.copyfile(WEB / "index.html", HERE / "previous-index.html")
    save("publish-intent.json", {"revision": manifest["revision"], "protected_pids": before})
    names = [p for p in manifest["files"] if p != "index.html"] + ["index.html"]
    for name in names:
        target = WEB / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".new")
        shutil.copyfile(HERE / "web-dist" / name, temporary)
        os.replace(temporary, target)
        assert sha(target) == manifest["files"][name]
    assert pids() == before
    for port in [8872, 8874]:
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(get(port, name)).hexdigest() == digest
    save(
        "published.json",
        {
            "revision": manifest["revision"],
            "ports": [8872, 8874],
            "web_root": str(WEB),
            "http_static_sha_verified": True,
            "protected_pids_unchanged": True,
            "backend_restarted": False,
            "files": manifest["files"],
        },
    )
    print((HERE / "published.json").read_text())


if __name__ == "__main__":
    publish()
