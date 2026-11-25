"""Read five original photos and stage only their missing caption candidates."""

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import repair_content as repair
from recover_videos import SCOPE, connect, save, sha
from stage_visual_review import crop_image

OUTPUT = repair.CONTENT / "partial-captions"
PROMPT = (
    "仅描述照片中实际可见的场景、主要物体与动作，最多120字。"
    "不读文字，不推断人物身份、地点名称、日期或意图；辨不清的细节保留不确定性。"
    '只返回JSON对象，且仅包含caption一个字段，例如：{"caption":"可见场景描述"}。'
)


def stage(identifier, asset):
    directory = OUTPUT / identifier
    directory.mkdir()
    data, metadata = crop_image(asset["path"], (0, 0, 1, 1))
    (directory / "image.jpg").write_bytes(data)
    save(
        directory / "image-metadata.json",
        {
            **metadata,
            "source_sha256": sha(asset["path"]),
            "image_sha256": sha(directory / "image.jpg"),
        },
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()},
                },
            ],
        }
    ]
    try:
        payload = repair.completion(directory / "model", messages, json_mode=True)
        candidate = json.loads(payload["choices"][0]["message"]["content"])
        if not (
            isinstance(candidate, dict)
            and set(candidate) == {"caption"}
            and isinstance(candidate["caption"], str)
            and 0 < len(candidate["caption"].strip()) <= 120
        ):
            raise repair.ModelOutputError("Missing or invalid caption candidate")
        save(directory / "candidate.json", candidate)
        return {"id": identifier, "status": "staged_for_review"}
    except (repair.ModelOutputError, json.JSONDecodeError) as error:
        return {"id": identifier, "status": "invalid_output", "error": str(error)}


def main():
    repair.assert_idle()
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    assert not OUTPUT.exists(), "Review existing evidence before requesting another candidate"
    original_plan = json.loads((repair.CONTENT / "plan.json").read_text())
    ids = original_plan["partial_caption_gaps_deferred"]
    assert len(ids) == len(set(ids)) == 5
    assets, observations, event_ids = {}, {}, {}
    with connect() as connection:
        for identifier in ids:
            asset = dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
            observation = dict(
                connection.execute("SELECT * FROM observations WHERE asset_id=?", (identifier,)).fetchone()
            )
            assert (
                asset["scope_id"] == observation["scope_id"] == SCOPE and not observation["caption"].strip()
            )
            assets[identifier], observations[identifier] = asset, observation
            event_ids[identifier] = [
                r[0]
                for r in connection.execute(
                    "SELECT event_id FROM event_observations WHERE observation_id=?", (observation["id"],)
                )
            ]
    OUTPUT.mkdir()
    save(
        OUTPUT / "intent.json",
        {
            "scope_id": SCOPE,
            "before": observations,
            "assets": assets,
            "event_ids": event_ids,
            "prompt": PROMPT,
            "model": repair.MODEL,
            "enable_thinking": False,
            "max_tokens": 4096,
            "workers": 4,
            "writes_memories": False,
            "source_sha256": {identifier: sha(asset["path"]) for identifier, asset in assets.items()},
        },
    )
    progress = {"status": "running", "started_at": time.time(), "results": []}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(stage, identifier, assets[identifier]) for identifier in ids]
            for future in as_completed(futures):
                progress["results"].append(future.result())
                save(OUTPUT / "progress.json", progress)
        for before in observations.values():
            assert repair.snapshot("observations", before["id"]) == before
        progress["invalid_outputs"] = sum(r["status"] == "invalid_output" for r in progress["results"])
        progress["status"] = "staged_for_review"
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(OUTPUT / "terminal.json", progress)
        print(json.dumps(progress))
    return 1 if progress["invalid_outputs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
