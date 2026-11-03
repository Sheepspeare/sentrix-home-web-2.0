"""Durable owner for the authorized matrix. Unknown submissions are never replayed."""

import base64
import concurrent.futures
import fcntl
import hashlib
import io
import json
import os
import subprocess
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
from PIL import Image

from scripts.benchmarks.mlx_capacity import (
    diagnostic_failure_categories,
    observe_run,
    percentile,
    save,
    submit_run_once,
    summarize_resources,
)

r = Path(os.environ["SENTRIX_MLX_EXPERIMENT_ROOT"]).resolve()
series = os.environ["SENTRIX_MLX_SERIES"]
if not series or any(
    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in series
):
    raise ValueError("series must be an explicit filesystem-safe label")

python = r / "repo/.venv/bin/python"
statefile = r / "runtime/matrix-state.json"
lock = (r / "runtime/matrix.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
state: dict[str, Any] = (
    json.loads(statefile.read_text()) if statefile.exists() else {"status": "starting", "rounds": []}
)
client = httpx.Client(timeout=30, trust_env=False)


def checkpoint(**kwargs: Any) -> None:
    state.update(kwargs, updated_at=time.time())
    save(statefile, state)


def owned_stop(pid: int, needle: str) -> None:
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    command = " ".join(p.cmdline())
    assert needle in command and str(r) in command, "process ownership differs"
    p.terminate()
    try:
        p.wait(45)
    except psutil.TimeoutExpired:
        p.kill()
        p.wait(10)


def stop_round() -> None:
    current = r / "runtime/current-round.json"
    if not current.exists():
        return
    d = json.loads(current.read_text())
    owned_stop(d["bench_pid"], "photobench.main:create_app")
    owned_stop(d["api_pid"], "sentrix.main:create_app")
    current.unlink()


def restart_engine() -> None:
    p = json.loads((r / "runtime/vllm-mlx-process.json").read_text())
    owned_stop(p["pid"], "serve_mlx.py")
    subprocess.run([str(python), str(r / "repo/scripts/benchmarks/launch_mlx_engine.py")], check=True)
    for _ in range(90):
        try:
            d = client.get("http://127.0.0.1:18100/v1/models")
            d.raise_for_status()
            return
        except httpx.HTTPError:
            time.sleep(2)
    raise RuntimeError("engine did not become ready")


def warmup() -> list[dict[str, Any]]:
    im = Image.new("RGB", (224, 224), "red")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def call(i: int) -> dict[str, Any]:
        content = (
            "Reply with exactly READY."
            if i % 2 == 0
            else [
                {"type": "text", "text": "Name the background color. One word."},
                {"type": "image_url", "image_url": {"url": url}},
            ]
        )
        body = {
            "model": "qwen35-4b-mlx-q4",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 64,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "seed": 42,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        result = client.post("http://127.0.0.1:18100/v1/chat/completions", json=body, timeout=240)
        result.raise_for_status()
        d = result.json()
        assert d["choices"][0]["finish_reason"] == "stop"
        return {"usage": d["usage"], "finish_reason": "stop"}

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        return list(pool.map(call, range(4)))


def summarize(entry: dict[str, Any]) -> dict[str, Any]:
    stage = r / "rounds" / entry["label"]
    rid = entry["run_id"]
    raw = json.loads((r / "results" / rid / "run.json").read_text())
    items = raw["items"]
    phase = raw["phases"]["qa_eval"]
    start = datetime.fromisoformat(phase["agent_phase_started_at"]).timestamp()
    end = datetime.fromisoformat(phase["agent_phase_finished_at"]).timestamp()
    samples = [json.loads(x) for x in (stage / "resources.jsonl").read_text().splitlines()]
    samples = [s for s in samples if start <= s["at"] <= end]
    events = [json.loads(x) for x in (r / "evidence/model-events.jsonl").read_text().splitlines()]
    events = [e for e in events if start <= e["at"] <= end]
    terminal = [e for e in events if e["event"] == "request_terminal"]
    resource = summarize_resources(samples)
    latencies = [i["wall_clock_ms"] / 1000 for i in items if isinstance(i.get("wall_clock_ms"), (int, float))]
    failures: Counter[str] = Counter()
    from photobench.domain.runs.execution import TASK_FAILURE_REASONS, agent_completion_error

    for item in items:
        reason = agent_completion_error(item)
        if reason:
            failures[
                reason if reason in TASK_FAILURE_REASONS or reason == "cancelled" else "agent_error"
            ] += 1
        if item.get("execution_status") != "completed":
            failures["execution_" + str(item.get("execution_status"))] += 1
        if item.get("judge_status") != "completed":
            failures["judge_" + str(item.get("judge_status"))] += 1
    for e in terminal:
        if e["finish_reason"] != "stop":
            failures["model_" + str(e["finish_reason"])] += 1
    if resource["engine_metrics_missing_samples"]:
        failures["missing_engine_metrics"] += resource["engine_metrics_missing_samples"]
    batches = [e["actual_batch_size"] for e in events if e["event"] == "token_batch"]
    ttfts = [e["ttft_s"] for e in terminal if e["ttft_s"] is not None]
    if not batches:
        failures["missing_actual_batch_evidence"] += 1
    qa = phase["agent_phase_wall_ms"] / 1000
    completed = sum(i.get("execution_status") == "completed" and not agent_completion_error(i) for i in items)
    value = {
        "label": entry["label"],
        "run_id": rid,
        "concurrency": entry["concurrency"],
        "questions": entry["questions"],
        "trial": entry.get("trial"),
        "completed": completed,
        "returned": sum(i.get("execution_status") == "completed" for i in items),
        "failure_categories": diagnostic_failure_categories(items),
        "judge_completed": sum(i.get("judge_status") == "completed" for i in items),
        "judge_failed": sum(i.get("judge_status") == "failed" for i in items),
        "status": raw["status"],
        "passed": raw.get("run_valid") is True and not failures and len(items) == entry["questions"],
        "qa_per_min": completed / qa * 60,
        "qa_seconds": qa,
        "judge_seconds": phase.get("judge_phase_wall_ms", 0) / 1000,
        "latency_p50_s": percentile(latencies, 0.5),
        "latency_p95_s": percentile(latencies, 0.95),
        "ttft_p95_s": percentile(ttfts, 0.95),
        "actual_batch_peak": max(batches) if batches else None,
        "queue_peak": resource["engine_num_requests_waiting_peak"],
        "memory_peak_gib": resource["memory_used_bytes"]["max"] / 2**30
        if resource["memory_used_bytes"]["max"] is not None
        else None,
        "swap_delta_gib": resource["swap_used_bytes_delta"] / 2**30
        if resource["swap_used_bytes_delta"] is not None
        else None,
        "failures": dict(failures),
        "samples": [
            {
                "seconds": s["at"] - start,
                "cpu": s.get("cpu_percent"),
                "gpu": s.get("gpu_percent"),
                "memory": s["memory_used_bytes"] / 2**30,
                "swap": s["swap_used_bytes"] / 2**30,
            }
            for s in samples
        ],
        "run_json_sha256": hashlib.sha256((r / "results" / rid / "run.json").read_bytes()).hexdigest(),
        "model_request_count": len(terminal),
        "resource_summary": resource,
        "ttft_p50_s": percentile(ttfts, 0.5),
        "judge_per_min": sum(i.get("judge_status") == "completed" for i in items)
        / (phase.get("judge_phase_wall_ms", 0) / 1000)
        * 60
        if phase.get("judge_phase_wall_ms", 0) > 0
        else None,
        "input_tokens_max": max((e["prompt_tokens"] for e in terminal), default=0),
        "run_valid": raw.get("run_valid"),
    }
    save(stage / "public-summary.json", value)
    return value


def publish() -> None:
    reports = []
    for entry in state["rounds"]:
        p = r / "rounds" / entry["label"] / "public-summary.json"
        if p.exists():
            reports.append(json.loads(p.read_text()))
    report = {
        "schema_version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "hardware": "Apple M4 · 16 GB · macOS 26.6",
        "engine": "vllm-mlx 0.6.0 / mlx-vlm 0.7.6",
        "model": "ModelScope Qwen3.5-4B-MLX-4bit",
        "model_sha256": "52dc943eaf6093a2313271a7a0abc36d127b2a16609a0a7c54ee1b4e4ed06cb8",
        "dataset_sha256": "73fa4512dde072fb0df9cf45ff3ebd192192db606f9764054bde002dcbf0be47",
        "context_tokens": json.loads((r / "private/photobench-config/runtime_connection.json").read_text())[
            "product_settings"
        ]["vlm_context_tokens"],
        "conditions": [
            "固定 64 题保留全部 69 轮对话；每轮恢复独立的冻结记忆库。",
            "关闭思考；30 个模型步骤；temperature 0.7 / top_p 0.8 / top_k 20 / "
            "presence_penalty 1.5 / seed 42。",
            "每轮重启推理引擎并进行同样的 4 请求预热；独立数据库、会话及模型缓存。",
            "QA 与云端 Judge 独立调度，评分排队不限制 QA。",
            "首 token 时间由模型调度器记录，包含模型队列等待；实际批次来自 token 生成批次。",
            "资源曲线与汇总限定 QA 阶段；原始记录保留完整运行及评分阶段。",
            "内存曲线采用 macOS 活跃及有线内存；PhotoBench 原始遥测另保留含缓存的整机占用。",
            "最高通过档要求两轮均完成、没有执行或评分错误，且监测证据完整。",
        ],
        "rounds": reports,
    }
    save(r / "results/capacity-report.json", report)
    web = r / "repo/apps/photobench/web/dist"
    if web.is_dir():
        save(web / "capacity-report.json", report)


def execute(label: str, c: int, questions: int) -> dict[str, Any]:
    entry = next((x for x in state["rounds"] if x["label"] == label), None)
    if entry and entry.get("status") == "completed":
        summary: dict[str, Any] = json.loads((r / "rounds" / label / "public-summary.json").read_text())
        if not summary["passed"]:
            raise RuntimeError("preserved failed round requires repair and a new series: " + label)
        return summary
    if entry and entry.get("status") == "observing" and entry.get("run_id"):
        stage = r / "rounds" / label
        checkpoint(status="running", current_round=label)
        observe_run(
            "http://127.0.0.1:18772", "http://127.0.0.1:18100", stage, entry["run_id"], deadline_s=86400
        )
        summary = summarize(entry)
        entry.update(status="completed", passed=summary["passed"])
        checkpoint()
        publish()
        if not summary["passed"]:
            raise RuntimeError("round failed; inspect preserved records: " + label)
        return summary
    if entry:
        raise RuntimeError("existing incomplete round needs explicit inspection; no automatic replay")
    entry = {
        "label": label,
        "concurrency": c,
        "questions": questions,
        "status": "preparing",
        "trial": int(label.split("-")[1][1:]) if "-R" in label else None,
    }
    state["rounds"].append(entry)
    checkpoint(status="running", current_round=label)
    stop_round()
    restart_engine()
    subprocess.run([str(python), str(r / "repo/scripts/benchmarks/prepare_mlx_round.py"), label], check=True)
    stage = r / "rounds" / label
    save(stage / "warmup.json", warmup())
    identity = json.loads((r / "source200/runtime/memory-baseline-identity.json").read_text())
    payload = {
        "album_id": "album3-max-video10",
        "qa_set": "mlx-fixed64" if questions == 64 else "mlx-full487",
        "models": ["__current__"],
        "endpoint_model": "qwen35-4b-mlx-q4",
        "mode": "reuse",
        "existing_scope_id": identity["scope_id"],
        "sentrix_url": "http://127.0.0.1:18191",
        "model_base_url": "http://127.0.0.1:18100/v1",
        "runtime_framework": "mlx",
        "qa_concurrency": c,
        "judge_backpressure": False,
        "turn_timeout": 4000,
    }
    entry["status"] = "submitting"
    checkpoint()
    run_id = submit_run_once(client, stage / "submission.json", payload)
    entry.update(run_id=run_id, status="observing")
    checkpoint()
    observe_run("http://127.0.0.1:18772", "http://127.0.0.1:18100", stage, entry["run_id"], deadline_s=86400)
    summary = summarize(entry)
    entry.update(status="completed", passed=summary["passed"])
    checkpoint()
    publish()
    if not summary["passed"]:
        raise RuntimeError("round failed; preserve records and repair before retesting: " + label)
    return summary


try:
    manifest = json.loads((r / "evidence/source-code-manifest.json").read_text())
    if not all(
        hashlib.sha256((r / "repo" / p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items()
    ):
        raise RuntimeError("frozen source manifest mismatch")
    pre = json.loads((r / "evidence/preflight-result.json").read_text())
    assert pre["status"] == "completed" and pre["run_valid"] is True
    assert json.loads((r / "evidence/window-seed-qualification.json").read_text())["window_passed"]
    assert json.loads((r / "evidence/engine-qualification.json").read_text())["all_passed"]
    for c in [4, 8, 12, 16]:
        for trial in [1, 2]:
            execute(f"C{c}-R{trial}-{series}", c, 64)
    passing = [
        c
        for c in [4, 8, 12, 16]
        if all(
            next(x for x in state["rounds"] if x["label"] == f"C{c}-R{trial}-{series}").get("passed")
            for trial in [1, 2]
        )
    ]
    if not passing:
        raise RuntimeError("no accepted concurrency tier")
    execute(f"C{max(passing)}-FULL487-{series}", max(passing), 487)
    checkpoint(status="evaluation_complete", highest_passing=max(passing))
    publish()
except BaseException as error:
    checkpoint(status="needs_attention", error=repr(error), traceback=traceback.format_exc())
    raise
finally:
    client.close()
