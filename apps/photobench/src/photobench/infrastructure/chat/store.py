"""Atomic, independent session files beside the evaluation result directory."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any


class FileChatStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"chat-[a-f0-9]{24}", session_id):
            raise KeyError(session_id)
        return self.root / f"{session_id}.json"

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = [self.load_session(path.stem) for path in self.root.glob("chat-*.json")]
        return sorted(sessions, key=lambda s: str(s["created_at"]), reverse=True)

    def load_session(self, session_id: str) -> dict[str, Any]:
        try:
            value: dict[str, Any] = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(session_id) from exc
        return value

    def delete_session(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink()
        except FileNotFoundError as exc:
            raise KeyError(session_id) from exc

    def save_session(self, session: dict[str, Any]) -> None:
        path = self._path(str(session["session_id"]))
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(session, handle, ensure_ascii=False)
                handle.flush()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
