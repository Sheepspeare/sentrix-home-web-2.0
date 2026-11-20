"""Verify the complete terminal Q4 HTTP exports without saving their bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from inspect_terminal import APP, BATCH, HERE, ROOT, RUN, api, current, fingerprint, save

from photobench.application.runs.reports import RunReports
from photobench.domain.evaluation.profile_view import primary_profile_state, profile_state
from photobench.domain.evaluation.profiles import result_key, valid_result
from photobench.domain.runs import exports
from photobench.infrastructure.results.split_store import SplitRunReader
from photobench.infrastructure.results.store import FileRunStore


def service_memory():
    pid = int(
        subprocess.check_output(
            ["systemctl", "--user", "show", "sentrix-atm100-photobench.service", "-p", "MainPID", "--value"],
            text=True,
        )
    )
    fields = dict(
        line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line
    )
    return {"pid": pid, **{k: fields[k].strip() for k in ("VmRSS", "VmHWM")}}


def progress(result, phase):
    value = {**result, "phase": phase, "updated_at": datetime.now().astimezone().isoformat()}
    (HERE / "exports-progress.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
    print(json.dumps({"phase": phase, "exports": result.get("exports", {})}), flush=True)


def verify():
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    assert not (HERE / "exports-acceptance.json").exists()
    accepted = json.loads((HERE / "scoring-acceptance.json").read_text())
    prepared = json.loads((HERE / "scoring-plan.json").read_text())
    assert accepted["valid_atm_scores"] == 978 and accepted["all_answers_and_attempts_unchanged"]
    assert accepted["unchanged_previous_valid_receipts"] == 963
    assert not api(8872, "active-work")["work"]
    reader, state, _ = current()
    revision = reader.revision
    assert revision == accepted["read_revision"]
    assert fingerprint(state["items"]) == prepared["all_items_reference_sha256"]
    selected = primary_profile_state(profile_state(state, "atm_qs_v1", BATCH))
    all_ids = {i["qa_id"] for i in state["items"]}
    assert len(all_ids) == len(state["items"]) == 1013
    failures = accepted["retained_execution_failures"]
    missing = {t["qa_id"] for t in accepted["remaining_targets"]}
    assert len(failures) == 34 and len(missing) == 1
    result = {
        "run_id": RUN,
        "worker_pid": os.getpid(),
        "read_revision": revision,
        "batch_id": BATCH,
        "qa_count": 1013,
        "valid_scores": 978,
        "retained_execution_failures": sorted(failures),
        "retained_judge_failures": sorted(missing),
        "exports": {},
        "service_memory_before": service_memory(),
        "export_bodies_saved": False,
    }
    projected_scores = samples = steps = quarantined = 0
    progress(result, "contracts")
    for item in selected["items"]:
        one = {**selected, "items": [item]}
        trace = exports.export_traces(one)
        sft = exports.export_sft(one)
        assert len(trace) == len(sft["items"]) == 1
        row = sft["items"][0]
        qa = item["qa_id"]
        assert trace[0]["qa_id"] == row["qa_id"] == qa
        assert row["answer"] == item.get("answer")
        assert row["termination_reason"] == item.get("termination_reason")
        score = state["scoring_results"].get(result_key(BATCH, qa, 0)) or {}
        for output in (trace[0], row):
            actual = output["profile_scores"]["atm_qs_v1"]
            assert actual["batch_id"] == BATCH
            assert actual["score"] == (score["score"] if valid_result(score) else None)
        if qa in failures:
            assert row["termination_reason"] == failures[qa]["termination_reason"]
        if qa in failures or qa in missing:
            assert row["profile_scores"]["atm_qs_v1"]["score"] is None
        projected_scores += int(valid_result(score))
        quarantined += int(bool(item.get("trace_quarantine")))
        samples += sft["count"]
        steps += sum(len(t["steps"]) for t in row["turns"])
    assert projected_scores == accepted["valid_atm_scores"]
    assert quarantined == 18
    result.update(
        sft_samples=samples,
        sft_steps=steps,
        contract_checked_items=1013,
        retained_quarantine_markers=quarantined,
    )
    reports = RunReports(FileRunStore(ROOT), None, lambda: "")
    for kind in ("trace", "sft"):
        started = time.monotonic()
        expected_hash, expected_bytes = hashlib.sha256(), 0
        progress(result, "expected_" + kind)
        for fragment in reports.stream_export(
            RUN, sft=kind == "sft", scoring_profile="atm_qs_v1", scoring_batch=BATCH
        ):
            raw = fragment.encode("utf-8")
            expected_hash.update(raw)
            expected_bytes += len(raw)
        expected_seconds = time.monotonic() - started
        assert SplitRunReader(ROOT, ROOT / RUN).revision == revision
        progress({**result, "expected_bytes": expected_bytes}, "http_" + kind)
        actual_hash, actual_bytes = hashlib.sha256(), 0
        started = time.monotonic()
        url = (
            f"http://127.0.0.1:8872/api/v1/runs/{RUN}/export-{kind}"
            f"?scoring_profile=atm_qs_v1&scoring_batch={BATCH}"
        )
        with urllib.request.urlopen(url, timeout=300) as response:
            assert response.status == 200
            assert "application/json" in response.headers["Content-Type"]
            assert "attachment" in response.headers["Content-Disposition"]
            while chunk := response.read(65536):
                actual_hash.update(chunk)
                actual_bytes += len(chunk)
        assert actual_bytes == expected_bytes
        assert actual_hash.hexdigest() == expected_hash.hexdigest()
        assert SplitRunReader(ROOT, ROOT / RUN).revision == revision
        result["exports"][kind] = {
            "bytes": actual_bytes,
            "sha256": actual_hash.hexdigest(),
            "expected_sha256": expected_hash.hexdigest(),
            "http_seconds": time.monotonic() - started,
            "expected_seconds": expected_seconds,
            "verified": True,
        }
        progress(result, "verified_" + kind)
    assert not api(8872, "active-work")["work"]
    final_reader, final_state, _ = current()
    assert final_reader.revision == revision
    assert fingerprint(final_state["items"]) == prepared["all_items_reference_sha256"]
    previous = {k: final_state["scoring_results"][k] for k in prepared["valid_score_keys"]}
    assert fingerprint(previous) == prepared["valid_receipts_reference_sha256"]
    result.update(
        status="verified_with_preserved_failures",
        finished_at=datetime.now().astimezone().isoformat(),
        service_memory_after=service_memory(),
        validator_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        answers_attempts_and_previous_scores_unchanged=True,
    )
    assert result["service_memory_after"]["pid"] == result["service_memory_before"]["pid"]
    save("exports-acceptance.json", result)
    progress(result, "completed")


def launch(wait_id):
    uuid.UUID(wait_id)
    assert not (HERE / "exports-intent.json").exists()
    assert not (HERE / "exports-acceptance.json").exists()
    accepted = json.loads((HERE / "scoring-acceptance.json").read_text())
    assert accepted["valid_atm_scores"] == 978
    assert not api(8872, "active-work")["work"]
    assert SplitRunReader(ROOT, ROOT / RUN).revision == accepted["read_revision"]
    command = [sys.executable, str(Path(__file__).resolve()), "verify"]
    save("exports-intent.json", {"wait_id": wait_id, "run_id": RUN, "command": command})
    with (HERE / "exports-worker.log").open("xb") as output:
        worker = subprocess.Popen(
            command,
            cwd=HERE,
            env={**os.environ, "PYTHONPATH": str(APP)},
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    response = {
        "wait_id": wait_id,
        "run_id": RUN,
        "worker_pid": worker.pid,
        "started_at": datetime.now().astimezone().isoformat(),
        "read_revision": accepted["read_revision"],
        "evaluation_pid": service_memory()["pid"],
    }
    save("exports-response.json", response)
    print(json.dumps(response), flush=True)


def probe(worker_pid):
    response = json.loads((HERE / "exports-response.json").read_text())
    assert worker_pid == response["worker_pid"]
    process = Path(f"/proc/{worker_pid}/cmdline")
    command = process.read_bytes().decode().split("\0") if process.exists() else []
    worker_alive = str(Path(__file__).resolve()) in command and "verify" in command
    acceptance = HERE / "exports-acceptance.json"
    result = (
        json.loads((HERE / "exports-progress.json").read_text())
        if (HERE / "exports-progress.json").exists()
        else {}
    )
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {
        "run_id": RUN,
        "worker_pid": worker_pid,
        "worker_alive": worker_alive,
        "phase": result.get("phase"),
        "exports": result.get("exports", {}),
        "accepted": acceptance.exists(),
        "evaluation_pid_matches": service_memory()["pid"] == response["evaluation_pid"],
        "revision_matches": SplitRunReader(ROOT, ROOT / RUN).revision == response["read_revision"],
        "disk_free": {p: shutil.disk_usage(p).free for p in ("/home", "/var/tmp")},
        "memory_available_kib": int(memory["MemAvailable"].split()[0]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("operation", choices=("launch", "verify", "probe"))
    parser.add_argument("--wait-id")
    parser.add_argument("--worker-pid", type=int)
    args = parser.parse_args()
    if args.operation == "launch":
        launch(args.wait_id)
    elif args.operation == "verify":
        verify()
    else:
        print(json.dumps(probe(args.worker_pid)), flush=True)
