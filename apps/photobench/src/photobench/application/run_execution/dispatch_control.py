"""File control for admission between questions; running turns keep their budgets."""

import json
from pathlib import Path


class DispatchControl:
    def __init__(self, path: str, concurrency: int):
        self.path = Path(path) if path else None
        self.concurrency = concurrency
        self.paused = False
        self.history: list[dict[str, object]] = []

    def admission(self, *, completed: int, inflight: int) -> bool:
        config = json.loads(self.path.read_text()) if self.path else {}
        requested = config.get("concurrency", self.concurrency)
        if type(requested) is not int or not min(2, self.concurrency) <= requested <= self.concurrency:
            raise ValueError("dispatch concurrency may only decrease, with minimum 2")
        pause_after = config.get("pause_after_completed")
        if pause_after is not None and (type(pause_after) is not int or pause_after < 1):
            raise ValueError("pause_after_completed must be a positive integer")
        paused = pause_after is not None and completed >= pause_after
        if requested != self.concurrency or paused != self.paused:
            self.history.append({"completed": completed, "concurrency": requested, "paused": paused})
        self.concurrency = requested
        self.paused = paused
        return inflight < requested and (pause_after is None or completed + inflight < pause_after)
