"""File result adapters with additive, generation-based atomic snapshot commits.

Legacy run.json/results.jsonl remain readable. New writes publish a single commit
pointer; root files are compatible exports, not the authoritative transaction.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Iterator
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

import ijson
from ijson.common import ObjectBuilder

from photobench.domain.datasets.qa_taxonomy import snapshot_metadata
from photobench.domain.runs.analysis import effective_summary, run_list_entry
from photobench.domain.runs.task_history import execution_metadata
from photobench.infrastructure.results.file_copy import copy_export

log = logging.getLogger(__name__)
# Fixed stripes bound lock memory, including deleted and never-seen run IDs.
_LOCKS = tuple(threading.RLock() for _ in range(64))


def run_lock(run_dir: Path) -> threading.RLock:
    return _LOCKS[hash(str(run_dir.resolve())) % len(_LOCKS)]


def committed_path(run_dir: Path, name: str) -> Path:
    """Call under run_lock. One pointer selects an immutable, complete generation."""
    pointer = run_dir / "snapshot-commit.json"
    if not pointer.exists():
        return run_dir / name
    generation = json.loads(pointer.read_text(encoding="utf-8"))["generation"]
    if not isinstance(generation, str) or len(generation) != 32 or not generation.isalnum():
        raise ValueError("invalid snapshot generation")
    return run_dir / ".snapshots" / generation / name


def load_snapshot_json(source: BinaryIO) -> Any:
    """Build the saved graph without a full UTF-32 copy of mixed image/text JSON."""
    builder = ObjectBuilder()
    try:
        for event, value in ijson.basic_parse(source):
            # Keep json.loads number types, including arbitrary-size integers.
            # use_float=True in the native parser would overflow large integers.
            builder.event(event, float(value) if isinstance(value, Decimal) else value)
        return builder.value
    except ijson.JSONError as error:
        raise ValueError("Invalid result snapshot JSON") from error
    finally:
        # ObjectBuilder's initial setter closes over itself. Release that cycle
        # immediately, so a finished request does not await cyclic GC for its GBs.
        builder.containers.clear()


def _durable_json(path: Path, value: Any) -> None:
    """Encode to the file so image payloads never form one giant Unicode string."""
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())


def _durable_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            json.dump(row, handle, ensure_ascii=False)
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _overview(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in state.items()
        if key not in {"items", "scoring_results", "diagnostic_results"}
    }
    if isinstance(result.get("qa_snapshot"), dict):
        result["qa_snapshot"] = snapshot_metadata(result["qa_snapshot"])
    result["scoring_batches"] = [
        {k: v for k, v in b.items() if k not in {"materials", "targets"}}
        for b in state.get("scoring_batches") or []
    ]
    result["item_count"] = len(state.get("items") or [])
    result["summary"] = effective_summary(state)
    return result


def read_entry(run_dir: Path) -> dict[str, Any]:
    """Cheap committed metadata; legacy derived index is invalidated by file identity."""
    with run_lock(run_dir):
        metadata = committed_path(run_dir, "metadata.json")
        if metadata.is_file():
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            entry = payload["entry"]
            if not isinstance(entry, dict):
                raise ValueError("invalid committed entry")
            # 已提交元数据中的旧列表投影可能缺少题型汇总；从同一快照概览补齐。
            evaluation = (payload.get("overview", {}).get("summary") or {}).get("evaluation_summary")
            if evaluation is not None:
                entry.setdefault("summary", {})["evaluation_summary"] = evaluation
            entry.update(execution_metadata(payload.get("overview") or {}))
            return entry
        path = run_dir / "run.json"
        stat = path.stat()
        signature = [3, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        index = run_dir / ".photobench-read-index.json"
        try:
            cached = json.loads(index.read_text(encoding="utf-8"))
            if cached.get("signature") == signature and isinstance(cached.get("entry"), dict):
                entry = cached["entry"]
                assert isinstance(entry, dict)
                return entry
        except (OSError, ValueError, KeyError, TypeError):
            pass
        with path.open("rb") as source:
            state = load_snapshot_json(source)
        if not isinstance(state, dict) or not state.get("run_id"):
            raise ValueError("missing run_id")
        entry = run_list_entry(state)
        # Derived only. Read-only historical directories remain readable.
        temporary = index.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({"signature": signature, "entry": entry}, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(index)
        except OSError:
            log.warning("Could not cache historical run entry: %s", run_dir, exc_info=True)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return entry


def write_committed_snapshot(run_dir: Path, state: dict[str, Any]) -> None:
    """Stage/fsync the complete pair, then atomically publish one commit pointer.

    Root files are legacy exports. Their repair never changes the committed
    revision. Readers and recovery use the pointer, including after a crash.
    """
    with run_lock(run_dir):
        run_dir.mkdir(parents=True, exist_ok=True)
        generation = uuid.uuid4().hex
        stages = run_dir / ".snapshots"
        stage = stages / generation
        stage.mkdir(parents=True)
        pointer = run_dir / "snapshot-commit.json"
        previous = json.loads(pointer.read_text())["generation"] if pointer.exists() else None
        temporary = run_dir / "snapshot-commit.tmp"
        try:
            _durable_json(stage / "run.json", state)
            _durable_jsonl(stage / "results.jsonl", state.get("items") or [])
            _durable_json(
                stage / "metadata.json",
                {"entry": run_list_entry(state), "overview": _overview(state)},
            )
            _sync_directory(stage)
            _sync_directory(stages)
            _durable_json(temporary, {"version": 1, "generation": generation})
            temporary.replace(pointer)
        except BaseException:
            temporary.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)
            raise
        # From here the commit is published; export/cleanup errors must not
        # pretend that the authoritative write was rolled back.
        # Failure to sync the published pointer is a durability failure: retain
        # the selected generation, but let the caller report uncertain persistence.
        _sync_directory(run_dir)
        try:
            for name in ("run.json", "results.jsonl"):
                export = run_dir / (name + ".tmp")
                copy_export(stage / name, export)
                export.replace(run_dir / name)
        except OSError:
            log.exception(
                "Snapshot %s committed; legacy export/cleanup needs repair: %s", generation, run_dir
            )
        finally:
            # Export failure must not bypass rolling-checkpoint cleanup forever.
            try:
                obsolete = list(stages.iterdir())
            except OSError:
                obsolete = []
                log.warning("Could not list obsolete checkpoints: %s", stages, exc_info=True)
            for old in obsolete:
                if old.is_dir() and old.name not in {generation, previous}:
                    try:
                        shutil.rmtree(old)
                    except OSError:
                        log.warning("Could not clean obsolete checkpoint: %s", old, exc_info=True)
            for name in ("run.json.tmp", "results.jsonl.tmp"):
                with suppress(OSError):
                    (run_dir / name).unlink(missing_ok=True)


class FileRunStore:
    def __init__(self, results_root: Path) -> None:
        self._root = results_root

    def _run_dir(self, run_id: str) -> Path:
        """run_id 直接来自 URL，所以先挡住路径穿越。

        1.0 是 ``self.results_root / run_id``（``:4974``），``run_id`` 里带 ``..``
        就能读到别处的 JSON。这里把非法形状当成「这个 run 不存在」，与 1.0 对一个
        普通未知 run_id 的结局（404）一致。
        """
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise KeyError(run_id)
        return self._root / run_id

    def iter_entries(self) -> Iterator[dict[str, Any]]:
        if not self._root.exists():
            return
        for run_dir in sorted(self._root.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            try:
                yield read_entry(run_dir)
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("Could not index run: %s", run_dir, exc_info=True)

    def load_state(self, run_id: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        with run_lock(run_dir):
            path = committed_path(run_dir, "run.json")
            if not path.is_file():
                raise KeyError(run_id)
            # Pin the selected inode before releasing the commit lock. A later
            # generation (or pruning this path) cannot change the open snapshot.
            source = path.open("rb")
        with source:
            value = load_snapshot_json(source)
        if not isinstance(value, dict):
            raise KeyError(run_id)
        return value

    def load_overview(self, run_id: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        with run_lock(run_dir):
            path = committed_path(run_dir, "metadata.json")
            if path.is_file():
                overview = json.loads(path.read_text(encoding="utf-8"))["overview"]
                if not isinstance(overview, dict):
                    raise ValueError("invalid committed overview")
                if "scoring" not in (overview.get("summary") or {}):
                    overview = _overview(self.load_state(run_id))
            else:
                overview = _overview(self.load_state(run_id))
            live_path = run_dir / "telemetry-live.json"
            if not overview.get("finished_at") and overview.get("status") in {
                "pending",
                "running",
                "cancelling",
            }:
                try:
                    view = json.loads(live_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    view = None
                if (
                    isinstance(view, dict)
                    and view.get("run_id") == run_id
                    and view.get("started_at")
                    and view["started_at"] == overview.get("started_at")
                ):
                    overview["telemetry_live"] = view["telemetry_live"]
                    phases = overview.setdefault("phases", {})
                    if (phases.get("gpu_metrics") or {}).get("status") not in {"done", "skipped"}:
                        phases["gpu_metrics"] = view["gpu_metrics"]
                    if view.get("llama_server_args"):
                        overview["llama_server_args"] = view["llama_server_args"]
            return {**overview, "read_revision": self.revision(run_id)}

    def revision(self, run_id: str) -> str:
        run_dir = self._run_dir(run_id)
        with run_lock(run_dir):
            path = committed_path(run_dir, "run.json")
            try:
                stat = path.stat()
            except FileNotFoundError as exc:
                raise KeyError(run_id) from exc
            return f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"

    def telemetry_revision(self, run_id: str) -> str:
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise KeyError(run_id)
        signatures = []
        for name in ("gpu_samples.jsonl", "telemetry-live.json"):
            try:
                stat = (run_dir / name).stat()
            except FileNotFoundError:
                signatures.append("absent")
            else:
                signatures.append(f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}")
        return "|".join(signatures)

    def read_reviews(self, run_id: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise KeyError(run_id)
        path = run_dir / "reviews.json"
        if not path.is_file():
            return {"reviews": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return {"reviews": value.get("reviews") or {}, "updated_at": value.get("updated_at")}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {"reviews": {}}

    def read_gpu_sample_lines(self, run_id: str) -> list[str]:
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise KeyError(run_id)
        path = run_dir / "gpu_samples.jsonl"
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8").splitlines()


class FileResultWriter:
    """Write complete checkpoints and append raw telemetry.

    A checkpoint still contains all items. Trace/event storage separation is a
    later change; raw history is never silently migrated or shortened here.
    """

    def __init__(self, results_root: Path) -> None:
        self._root = results_root

    def _run_dir(self, run_id: str) -> Path:
        """与 ``FileRunStore._run_dir`` 同一条路径穿越拦截。写入侧更需要它：
        读到一个越界的路径最多是读错文件，写进去是**覆盖别人的文件**。"""
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise KeyError(run_id)
        return self._root / run_id

    def write_snapshot(self, run_id: str, state: dict[str, Any]) -> None:
        run_dir = self._run_dir(run_id)
        write_committed_snapshot(run_dir, state)

    def write_telemetry_view(self, run_id: str, view: dict[str, Any]) -> None:
        run_dir = self._run_dir(run_id)
        with run_lock(run_dir):
            run_dir.mkdir(parents=True, exist_ok=True)
            target = run_dir / "telemetry-live.json"
            temporary = run_dir / "telemetry-live.tmp"
            try:
                temporary.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def reset_gpu_samples(self, run_id: str) -> None:
        path = self._run_dir(run_id) / "gpu_samples.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        (path.parent / "telemetry-live.json").unlink(missing_ok=True)

    def append_gpu_sample(self, run_id: str, sample: dict[str, Any]) -> None:
        path = self._run_dir(run_id) / "gpu_samples.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")

    def save_reviews(self, run_id: str, stored: dict[str, Any]) -> None:
        """原子写 ``reviews.json``（1.0 ``save_reviews`` 的 ``atomic_json``）。"""
        run_dir = self._run_dir(run_id)
        with run_lock(run_dir):
            run_dir.mkdir(parents=True, exist_ok=True)
            target = run_dir / "reviews.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(target)

    def delete_run(self, run_id: str) -> None:
        """删掉整个 run 目录（1.0 ``:7010``）。

        ``_run_dir`` 已经挡住 ``..`` 与分隔符；这里再加一道「解析后的路径必须真的在
        ``results_root`` 之下」——符号链接也能把 ``results/x`` 指到别处，而 1.0 的
        ``self.results_root in run_dir.parents`` 正是这道校验。
        """
        run_dir = self._run_dir(run_id).resolve()
        root = self._root.resolve()
        if root not in run_dir.parents:
            raise KeyError(run_id)
        with run_lock(run_dir):
            if run_dir.exists():
                shutil.rmtree(run_dir)
