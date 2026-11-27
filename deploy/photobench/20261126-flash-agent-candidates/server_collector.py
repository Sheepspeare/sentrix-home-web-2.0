"""Export only completed common answers through GETs pinned to each item's revision."""

import copy
import hashlib
import json
import types
import urllib.parse
import urllib.request


def local_get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=90) as response:
        return json.load(response)


OP = types.SimpleNamespace(get=local_get)


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def read_content(port, run, index, path, revision, get=OP.get):
    offset, chunks, pages = 0, [], []
    while True:
        query = urllib.parse.urlencode({"path": path, "offset": offset, "limit": 65536, "revision": revision})
        value = get(port, f"runs/{run}/items/{index}/content?{query}")
        assert value["read_revision"] == revision, (path, "revision mismatch")
        text = value.get("text", "")
        pages.append(
            {
                "offset": offset,
                "next_offset": value.get("next_offset"),
                "read_revision": revision,
                "text_sha256": digest(text.encode()),
            }
        )
        chunks.append(text)
        following = value.get("next_offset")
        if following is None:
            return json.loads("".join(chunks)) if any(chunks) else None, pages
        assert following > offset, (path, "pagination did not advance")
        offset = following


def hydrate(item, port, run, get=OP.get):
    result = copy.deepcopy(item)
    revision, index = item["trace_storage"]["revision"], item["index"]
    paths = {"qa_id", "answer"}
    paths.update(field["path"] for field in item.get("content_fields", []))
    paths.update(
        f"runtime_turns.{n}.answer"
        for n, turn in enumerate(item.get("runtime_turns") or [])
        if "answer" in turn
    )
    checks = {}
    for path in sorted(paths):
        value, pages = read_content(port, run, index, path, revision, get)
        target = result
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[int(parts[-1]) if isinstance(target, list) else parts[-1]] = value
        checks[path] = {
            "pages": pages,
            "value_sha256": digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()),
        }
    assert result["qa_id"] == item["qa_id"]
    assert result["execution_status"] == "completed"
    for field in result.pop("content_fields", []):
        target = result
        for part in field["path"].split("."):
            target = target[int(part)] if isinstance(target, list) else target[part]
        assert len(target) == field["characters"]
    return result, checks
