"""Wait for direct Judge completion or attention without changing either worker."""

import argparse
import json
import subprocess
import time
from pathlib import Path

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "192.168.0.100"]


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def watch(root):
    plan = json.loads((root / "remote-plan.json").read_text())
    assert not (root / "observer-started.json").exists(), "Read prior observer before attaching"
    save(root / "observer-started.json", {"at": time.time(), "remote": plan["remote"], "read_only": True})
    errors = []
    names = [
        "started",
        "progress",
        "concurrency",
        "terminal",
        "failure",
        "limited-attention",
        "large-attention",
    ]
    while True:
        values = {}
        for name in names:
            path = plan["remote"] + "/" + name + ".json"
            try:
                text = subprocess.check_output(
                    [*SSH, "test ! -f '" + path + "' || cat '" + path + "'"], text=True, timeout=40
                ).strip()
                if text:
                    values[name] = json.loads(text)
                    save(root / ("remote-" + name + ".json"), values[name])
            except Exception as error:
                errors.append({"at": time.time(), "file": name, "error": str(error)})
                save(root / "observer-read-errors.json", errors)
                if len(errors) >= 6:
                    raise
        if any(name in values for name in ["failure", "limited-attention", "large-attention"]):
            save(
                root / "observer-terminal.json",
                {"at": time.time(), "status": "needs_review", "values": values},
            )
            return
        if "terminal" in values:
            save(root / "observer-terminal.json", {"at": time.time(), **values["terminal"]})
            return
        if time.time() >= plan["report_at"]:
            save(
                root / "observer-terminal.json",
                {"at": time.time(), "status": "report_checkpoint", "values": values},
            )
            return
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    root = parser.parse_args().root
    try:
        watch(root)
    except BaseException as error:
        save(root / "observer-terminal.json", {"status": "failed", "at": time.time(), "error": str(error)})
        raise
