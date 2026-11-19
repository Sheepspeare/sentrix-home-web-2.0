"""Start the authorized independent Doubao full run only after Qwen export acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from inspect_terminal import BASE, ROOT, RUN

from photobench.infrastructure.results.split_store import SplitRunReader

HERE = Path(__file__).resolve().parent
ALBUM = "2026.11.17-atm-sources-addresses"
PACKAGE = Path("/var/tmp/sentrix-atm-sources-20261117") / ALBUM
QA_SHA = "13d07b33d8e9c522347bf485539a9a71d8ce569385d1ecf26620218b7e3383c3"
MODEL = "doubao-seed-2.1-lite"
DATABASE = "/var/tmp/sentrix-atm-doubao-20261112/product-data/sentrix.db"
PROTECTED = (
    "sentrix-atm100-api.service",
    "sentrix-atm100-photobench.service",
    "sentrix-atm100-model.service",
    "sentrix2-api.service",
    "sentrix2-photobench.service",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def api(port, path, data=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/" + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180 if data is not None else 60) as response:
        return json.load(response)


def save(name, value):
    with (HERE / name).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def protected_pids():
    return {
        unit: subprocess.check_output(
            ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True
        ).strip()
        for unit in PROTECTED
    }


def preflight():
    accepted = json.loads((HERE / "exports-acceptance.json").read_text())
    assert accepted["status"] == "verified" and accepted["qa_count"] == 1013
    assert accepted["valid_scores"] == 1012 and all(e["verified"] for e in accepted["exports"].values())
    reader = SplitRunReader(ROOT, ROOT / RUN)
    assert reader.revision == accepted["read_revision"]
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping["qwen37"]["run_id"] == RUN and "doubao" not in mapping
    assert not api(8874, "active-work")["work"]
    imports = api(8294, "imports/progress")
    assert not imports["library"]["processing"] and imports["postprocess"]["state"] == "idle"
    with sqlite3.connect("file:" + DATABASE + "?mode=ro", uri=True) as db:
        tables = {}
        for table in ("agent_turns", "import_jobs", "video_jobs", "pipeline_receipts"):
            columns = [r[1] for r in db.execute(f"pragma table_info({table})")]
            assert "status" in columns
            counts = dict(db.execute(f"select status,count(*) from {table} group by status"))
            tables[table] = counts
            active = (
                "accepted",
                "queued",
                "pending",
                "running",
                "processing",
                "indexing",
                "extracting",
                "waiting_dependency",
                "retry_scheduled",
            )
            assert not any(counts.get(s) for s in active), (table, counts)
        assert (
            db.execute(
                "select count(*) from scope_postprocess where processing_generation is not null"
            ).fetchone()[0]
            == 0
        )
    assert sha(PACKAGE / "qa/atm-bench.jsonl") == QA_SHA
    assert len((PACKAGE / "qa/atm-bench.jsonl").read_text().splitlines()) == 1013
    manifest = json.loads((PACKAGE / "manifest.json").read_text())
    assert len(manifest["photos"]) == 3759 and len(manifest["videos"]) == 533
    assert all((PACKAGE / name).is_file() for name in manifest["photos"] + manifest["videos"])
    assert len(json.loads((PACKAGE / "originals/emails.json").read_text())) == 6742
    frozen = json.loads((BASE / "scoring-frozen.json").read_text())
    assert frozen["judge_model"] == MODEL and frozen["scoring_profiles"] == ["atm_qs_v1"]
    assert not frozen["scoring_configs"]["atm_qs_v1"]["use_gt_images"]
    assert not frozen["scoring_configs"]["atm_qs_v1"]["use_gt_metadata"]
    diagnostics = api(8294, "diagnostics")
    runtime = diagnostics["runtime_configuration"]
    assert [runtime["reasoning_max_" + k + "_calls"] for k in ("model", "tool", "search", "other_tool")] == [
        100,
        600,
        100,
        500,
    ]
    with urllib.request.urlopen("http://127.0.0.1:18107/v1/models", timeout=20) as response:
        models = json.load(response)
    assert models["data"][0]["id"] == MODEL
    free = {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")}
    assert free["/home"] > 7 * 1024**3 and free["/var/tmp"] > 6 * 1024**3
    q4 = api(8872, "active-work")["work"]
    assert any(w["id"] == mapping["q4km"]["run_id"] and w["worker_alive"] for w in q4)
    payload = {
        **frozen,
        "album_id": ALBUM,
        "qa_set": "atm-bench",
        "models": ["__current__"],
        "mode": "full",
        "model_base_url": "http://127.0.0.1:18107/v1",
        "endpoint_model": MODEL,
        "vllm_manager_url": "",
        "sentrix_url": "http://127.0.0.1:8294",
        "reasoning": True,
        "graph_enabled": True,
        "qa_concurrency": 4,
        "turn_timeout": 3660,
        "pipeline_timeout": 172800,
        "import_timeout": 172800,
    }
    return {
        "qwen_run_id": RUN,
        "qwen_revision": reader.revision,
        "qwen_scope": reader.overview()["scope_id"],
        "mapping_before": mapping,
        "qwen_exports_sha256": sha(HERE / "exports-acceptance.json"),
        "qa_sha256": QA_SHA,
        "scoring_frozen_sha256": sha(BASE / "scoring-frozen.json"),
        "model_discovery": models,
        "product_counts": tables,
        "imports": imports,
        "product_settings": {k: v for k, v in runtime.items() if "reasoning_max_" in k},
        "product_build_revision": diagnostics["build_revision"],
        "disk_free": free,
        "protected_pids": protected_pids(),
        "payload": payload,
    }


def plan():
    value = preflight()
    save("doubao-plan.json", value)
    print(
        json.dumps(
            {
                "status": "ready",
                "qwen_exports_accepted": True,
                "model": MODEL,
                "mode": "full",
                "qa_count": 1013,
                "disk_free": value["disk_free"],
            }
        ),
        flush=True,
    )


def launch(wait_id):
    uuid.UUID(wait_id)
    assert not (HERE / "doubao-launch-intent.json").exists()
    prior = json.loads((HERE / "doubao-plan.json").read_text())
    now = preflight()
    for field in (
        "qwen_revision",
        "mapping_before",
        "qwen_exports_sha256",
        "qa_sha256",
        "scoring_frozen_sha256",
        "model_discovery",
        "protected_pids",
        "payload",
    ):
        assert now[field] == prior[field], field
    save("doubao-launch-intent.json", {"wait_id": wait_id, "at": time.time(), **now})
    response = api(8874, "runs", now["payload"])
    save("doubao-launch-response.json", response)
    assert len(response["run_ids"]) == 1 and response["mode"] == "full"
    run_id = response["run_ids"][0]
    assert run_id not in (RUN, now["mapping_before"]["q4km"]["run_id"])
    mapping = json.loads((BASE / "active-runs.json").read_text())
    assert mapping == now["mapping_before"]
    mapping["doubao"] = {
        "run_id": run_id,
        "mode": "full",
        "endpoint_model": MODEL,
        "wait_id": wait_id,
        "launch_receipt": str(HERE / "doubao-launch-response.json"),
    }
    pending = BASE / "active-runs.doubao-new.pending"
    with pending.open("x") as stream:
        json.dump(mapping, stream, ensure_ascii=False, indent=2)
    pending.replace(BASE / "active-runs.json")
    save("doubao-active-mapping.json", mapping)
    assert protected_pids() == now["protected_pids"]
    print(json.dumps({"model": MODEL, "wait_id": wait_id, **response}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("plan", "launch"))
    parser.add_argument("--wait-id")
    args = parser.parse_args()
    plan() if args.operation == "plan" else launch(args.wait_id)
