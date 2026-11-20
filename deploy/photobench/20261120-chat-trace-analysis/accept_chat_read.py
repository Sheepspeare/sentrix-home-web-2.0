"""Exercise deployed chat reads in a temporary store using one saved product turn."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient

from photobench.api.routes.chat import router
from photobench.application.interactive.chat import ChatService
from photobench.infrastructure.chat.store import FileChatStore


class SavedTurnClient:
    """Expose only product GETs; generation methods are deliberately absent."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.reads: list[str] = []
        self.answer_sha256 = ""

    def read(self, path: str):
        self.reads.append(path)
        with urllib.request.urlopen(self.base_url + "/api/v1" + path, timeout=60) as response:
            return json.load(response)

    def get_turn(self, *, turn_id: str):
        turn = self.read("/assistant/turns/" + quote(turn_id, safe=""))
        assert turn["status"] in {"complete", "partial"}, turn["status"]
        self.answer_sha256 = digest((turn.get("result") or {}).get("answer") or "")
        return turn

    def get_asset(self, *, asset_id: str):
        return self.read("/assets/" + quote(asset_id, safe=""))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def accept(base_url: str, turn_id: str):
    started = time.monotonic()
    product = SavedTurnClient(base_url)
    with tempfile.TemporaryDirectory(prefix="chat-read-accept-", dir="/var/tmp") as temporary:
        store = FileChatStore(Path(temporary))
        session_id = "chat-" + "a" * 24
        store.save_session(
            {
                "session_id": session_id,
                "created_at": "2026-11-20T00:00:00+08:00",
                "scope_id": "isolated-read-acceptance",
                "status": "ready",
                "runtime_snapshot": {"sentrix_url": base_url},
                "turns": [
                    {
                        "index": 0,
                        "turn_id": turn_id,
                        "message": "既有产品回合只读验收",
                        "status": "partial",
                    }
                ],
            }
        )
        session_file = Path(temporary) / (session_id + ".json")
        before = session_file.read_bytes()
        service = ChatService(
            store=store,
            suites=None,
            albums=None,
            spaces=None,
            client_for=lambda _: product,
            execution_for=None,
        )
        app = FastAPI()
        app.state.container = SimpleNamespace(chat=service)
        app.include_router(router, prefix="/api/v1")
        with TestClient(app) as http:
            path = "/api/v1/chat/sessions/" + session_id
            light = http.get(path)
            assert light.status_code == 200, light.text[:200]
            assert "items" not in light.json() and not product.reads
            first = http.get(path + "/turns/0")
            assert first.status_code == 200, first.text[:200]
            answer_sha256 = digest(first.json()["answer"])
            assert answer_sha256 == product.answer_sha256
            reads = len(product.reads)
            again = http.get(path + "/turns/0")
            assert again.status_code == 200 and digest(again.json()["answer"]) == answer_sha256
            assert len(product.reads) == reads, "Terminal cache triggered a second product read"
            nodes = http.get(path + "/turns/0/trace", params={"kind": "calls", "limit": 1})
            assert nodes.status_code == 200 and nodes.json()["nodes"]
            page = nodes.json()
            timing = http.get(
                path + "/turns/0/trace/" + page["nodes"][0]["node_id"],
                params={"view": "timing", "revision": page["read_revision"]},
            )
            assert timing.status_code == 200 and "node" in timing.json()
            assert len(product.reads) == reads
        assert session_file.read_bytes() == before
        result = {
            "turn_id": turn_id,
            "session_http": light.status_code,
            "turn_http": first.status_code,
            "trace_http": nodes.status_code,
            "timing_http": timing.status_code,
            "session_product_reads": 0,
            "turn_product_reads": sum(p.startswith("/assistant/turns/") for p in product.reads),
            "asset_product_reads": sum(p.startswith("/assets/") for p in product.reads),
            "cache_additional_product_reads": len(product.reads) - reads,
            "answer_sha256": answer_sha256,
            "source_answer_preserved": True,
            "session_file_preserved": True,
            "trace_node_count": page["total"],
            "model_requests": 0,
            "production_chat_writes": 0,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    result["temporary_store_removed"] = not Path(temporary).exists()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-url", required=True)
    parser.add_argument("--turn-id", required=True)
    args = parser.parse_args()
    print(json.dumps(accept(args.product_url, args.turn_id)), flush=True)
