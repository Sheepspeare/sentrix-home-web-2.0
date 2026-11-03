#!/usr/bin/env python3
"""Version-guarded vllm-mlx benchmark entry point (explicit window, request seed 42).

Upstream 0.6.0 has neither exact multimodal /tokenize nor a request seed.
This adapter uses its own preprocessing path and gives each sampler an explicit
MLX random key. It does not rotate KV, trim prompts, or serialize inference.
Only the fixed benchmark seed is accepted; other seeds fail visibly.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

WINDOW = int(os.environ.get("SENTRIX_MLX_CONTEXT_TOKENS", "32768"))
SEED = 42


def install_telemetry() -> None:
    """Record actual scheduler token batches and terminal timing, never content."""
    destination = os.environ.get("SENTRIX_MLX_EVENTS")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    scheduler: Any = importlib.import_module("vllm_mlx.mllm_scheduler").MLLMScheduler
    original_process = scheduler._process_batch_responses
    original_abort = scheduler.abort_request

    def emit(event: dict[str, Any]) -> None:
        with lock, path.open("a") as stream:
            stream.write(json.dumps({"at": time.time(), **event}) + "\n")

    def terminal(request: Any, reason: str) -> None:
        emit(
            {
                "event": "request_terminal",
                "request_id": request.request_id,
                "finish_reason": reason,
                "arrival_at": request.arrival_time,
                "ttft_s": request.first_token_time - request.arrival_time
                if request.first_token_time is not None
                else None,
                "latency_s": time.time() - request.arrival_time,
                "prompt_tokens": request.num_prompt_tokens,
                "completion_tokens": request.num_output_tokens,
            }
        )

    def process(instance: Any, responses: Any) -> Any:
        result = original_process(instance, responses)
        outputs, finished = result
        size = len({output.request_id for output in outputs})
        if size != getattr(instance, "_evidence_last_batch", None):
            emit(
                {
                    "event": "token_batch",
                    "actual_batch_size": size,
                    "scheduler_running": len(instance.running),
                    "scheduler_waiting": len(instance.waiting),
                }
            )
            instance._evidence_last_batch = size
        for request_id in finished:
            request = instance.running.get(request_id)
            if request is not None:
                terminal(request, request.finish_reason or "unknown")
        return result

    def abort(instance: Any, request_id: str) -> Any:
        request = instance.requests.get(request_id)
        result = original_abort(instance, request_id)
        if result and request is not None:
            terminal(request, "abort")
        return result

    scheduler._process_batch_responses = process
    scheduler.abort_request = abort


def seeded_sampler(
    temp: float = 0.0,
    top_p: float = 0.0,
    min_p: float = 0.0,
    min_tokens_to_keep: int = 1,
    top_k: int = 0,
    xtc_probability: float = 0.0,
    xtc_threshold: float = 0.0,
    xtc_special_tokens: list[int] | None = None,
) -> Callable[[Any], Any]:
    """Upstream filters, with independent RNG state instead of a shared RNG."""
    mx = importlib.import_module("mlx.core")
    sampling: Any = importlib.import_module("mlx_lm.sample_utils")
    if xtc_probability != 0:
        raise ValueError("XTC is outside the frozen benchmark sampling profile")
    key = mx.random.key(SEED)

    def sample(logprobs: Any) -> Any:
        nonlocal key
        if temp == 0:
            return mx.argmax(logprobs, axis=-1)
        if 0 < top_p < 1:
            logprobs = sampling.apply_top_p(logprobs, top_p)
        if min_p != 0:
            logprobs = sampling.apply_min_p(logprobs, min_p, min_tokens_to_keep)
        if top_k > 0:
            logprobs = sampling.apply_top_k(logprobs, top_k)
        key, draw = mx.random.split(key)
        return mx.random.categorical(logprobs * (1.0 / temp), key=draw)

    return sample


def exact_count(server: Any, body: dict[str, Any]) -> int:
    """Match BatchedEngine.chat + MLLMBatchGenerator preprocessing exactly."""
    engine = server._engine
    if engine is None or not engine._loaded or not engine.is_mllm:
        raise HTTPException(503, "multimodal engine is not ready")
    utils = importlib.import_module("vllm_mlx.api.utils")
    tool_calling = importlib.import_module("vllm_mlx.api.tool_calling")
    mllm = importlib.import_module("vllm_mlx.models.mllm")
    vlm = importlib.import_module("mlx_vlm.utils")
    models = importlib.import_module("vllm_mlx.api.models")
    request = models.ChatCompletionRequest.model_validate(body)
    prepared = server._prepare_chat_completion_invocation(engine, request, 1)
    messages = prepared.messages
    _, images, videos, audios = utils.extract_multimodal_content(messages)
    if videos or audios:
        raise HTTPException(422, "this benchmark accepts video keyframes as images, not native video/audio")
    template_kwargs = prepared.chat_kwargs.get("chat_template_kwargs") or {}
    if (
        template_kwargs.get("enable_thinking", False) is not False
        or prepared.chat_kwargs.get("enable_thinking", False) is not False
    ):
        raise HTTPException(422, "thinking must be disabled for this benchmark")
    prompt = engine._apply_chat_template(
        messages,
        tool_calling.convert_tools_for_template(prepared.chat_kwargs.get("tools")),
        num_images=len(images),
        chat_template_kwargs=template_kwargs,
        enable_thinking=False,
    )
    inputs = vlm.prepare_inputs(
        engine._processor,
        images=[mllm.process_image_input(item) for item in images] or None,
        audio=None,
        prompts=prompt,
        image_token_index=getattr(engine._model.config, "image_token_index", None),
    )
    return int(inputs["input_ids"].shape[-1])


def install_adapter() -> None:
    for package, expected in {"vllm-mlx": "0.6.0", "mlx-vlm": "0.7.6", "transformers": "5.19.0"}.items():
        if version(package) != expected:
            raise RuntimeError(f"{package} must be {expected}")
    sampling: Any = importlib.import_module("mlx_lm.sample_utils")
    sampling.make_sampler = seeded_sampler
    install_telemetry()
    server = importlib.import_module("vllm_mlx.server")
    app: FastAPI = server.app

    @app.post("/tokenize")
    async def tokenize(body: dict[str, Any]) -> dict[str, Any]:
        count = await run_in_threadpool(exact_count, server, body)
        return {"count": count, "max_model_len": WINDOW, "seed_policy": "per-request-42"}

    @app.middleware("http")
    async def enforce_window(request: Request, call_next: Any) -> Any:
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            try:
                body = await request.json()
                if body.get("seed", SEED) != SEED:
                    raise HTTPException(422, "only fixed benchmark seed 42 is supported")
                count = await run_in_threadpool(exact_count, server, body)
                output = body.get("max_tokens", WINDOW)
                if not isinstance(output, int) or output < 1 or count + output > WINDOW:
                    raise HTTPException(
                        400, f"context overflow: input={count}, output={output}, window={WINDOW}"
                    )
            except HTTPException as error:
                return JSONResponse({"error": {"message": error.detail}}, status_code=error.status_code)
            except (ValueError, KeyError, TypeError) as error:
                return JSONResponse({"error": {"message": str(error)}}, status_code=422)
        return await call_next(request)


if __name__ == "__main__":
    install_adapter()
    importlib.import_module("vllm_mlx.cli").main()
