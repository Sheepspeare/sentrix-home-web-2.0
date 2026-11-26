"""Cloud authentication, native parameters and HTTP evidence for frozen Agent code.

This adapter runs inside the two experimental processes. It has no listener,
queue, semaphore, retries or Agent logic. Requests go straight to the cloud URL.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

_LOCK = threading.Lock()


def native_body(body):
    result = dict(body)
    result.pop("id_slot", None)
    result.pop("chat_template_kwargs", None)
    result.pop("thinking", None)
    result["enable_thinking"] = False
    result["stream"] = False
    return result


def install():
    endpoint = os.environ.get("FLASH_DIRECT_URL", "").rstrip("/")
    if not endpoint:
        return
    target = urlsplit(endpoint)
    key = os.environ["QWEN_API_KEY"].strip()
    assert key and target.scheme == "https" and target.hostname not in {"localhost", "127.0.0.1"}
    output = Path(os.environ["FLASH_DIRECT_HTTP_DIR"])
    output.mkdir(parents=True, exist_ok=True)
    original = httpx.Client.send

    def send(client, request, **kwargs):
        url = urlsplit(str(request.url))
        if (url.scheme, url.netloc) != (target.scheme, target.netloc):
            return original(client, request, **kwargs)
        request.headers["Authorization"] = "Bearer " + key
        if not url.path.endswith("/chat/completions"):
            return original(client, request, **kwargs)
        body = native_body(json.loads(request.read()))
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        headers = dict(request.headers)
        headers.pop("content-length", None)
        request = httpx.Request(
            request.method, request.url, headers=headers, content=content, extensions=request.extensions
        )
        identity = uuid.uuid4().hex
        metadata = {
            "id": identity,
            "started_at": time.time(),
            "url": str(request.url),
            "direct_cloud": True,
            "model": body["model"],
            "enable_thinking": body["enable_thinking"],
            "max_tokens": body.get("max_tokens"),
            "request_sha256": hashlib.sha256(content).hexdigest(),
        }
        response = None
        try:
            response = original(client, request, **kwargs)
            response.read()
            metadata["http_status"] = response.status_code
            metadata["response_sha256"] = hashlib.sha256(response.content).hexdigest()
            try:
                payload = response.json()
                metadata["usage"] = payload.get("usage")
                metadata["response_id"] = payload.get("id")
            except ValueError:
                payload = response.text
            return response
        except BaseException as error:
            metadata["error_type"] = type(error).__name__
            metadata["error"] = str(error)
            payload = None
            raise
        finally:
            metadata["finished_at"] = time.time()
            archive = output / (identity + ".json.gz")
            with gzip.open(archive, "wt", encoding="utf-8") as stream:
                json.dump(
                    {"metadata": metadata, "request": body, "response": payload}, stream, ensure_ascii=False
                )
            metadata["archive"] = str(archive)
            with _LOCK, (output.parent / "requests.jsonl").open("a") as stream:
                stream.write(json.dumps(metadata, ensure_ascii=False) + "\n")

    httpx.Client.send = send
