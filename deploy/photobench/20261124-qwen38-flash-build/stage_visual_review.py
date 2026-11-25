"""Stage bounded visual evidence for four reviewed failures; never write memories."""

import base64
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import repair_content as repair
from PIL import Image, ImageOps
from recover_videos import connect, save, sha

# Fractions refer to the EXIF-oriented original. Crops are evidence, not replacements.
REGIONS = {
    "asset_78a95b5d74d8": {
        "description": (0, 0, 1, 1),
        "header": (0.18, 0, 0.9, 0.1),
        "menu_corner": (0, 0.1, 0.16, 0.34),
    },
    "asset_816c96df9415": {
        "description": (0, 0, 1, 1),
        "sign": (0.25, 0.66, 0.55, 0.87),
    },
    "asset_abd37b0e8d4c": {"notice": (0.28, 0.47, 0.85, 0.68)},
    "asset_75deb3249002": {"poster_header": (0.25, 0.42, 0.78, 0.49)},
}
DESCRIPTION_PROMPT = (
    "仅描述照片中实际可见的场景、物体与活动。不读字，不推断食品品种、身份或购买意图；"
    "辨不清的物体用上位类别。只返回JSON对象：caption（非空，最多120字）、"
    "activity（最多40字）、objects（最多12个可见物品）、clothing（最多6项）。"
    "不要输出OCR、招牌文字、价格、人名、日期或facts。"
)
OCR_PROMPT = (
    "这是一张从原图裁出的文字区域。逐字转录其中最清楚的至多6行文字，总共最多180字。"
    "按图中实际行顺序，不补全被裁掉、模糊或遮挡的字；看不清就少写或留空。"
    "保留原语言与数字，不翻译、不概括、不追加菜单品种、价格、作者或机构。"
    "只返回JSON对象：lines（字符串数组，至多6项），uncertain（布尔值）。"
)


def validate_candidate(payload, *, description):
    if description:
        assert isinstance(payload.get("caption"), str) and 0 < len(payload["caption"]) <= 120
        assert isinstance(payload.get("activity"), str) and len(payload["activity"]) <= 40
        for field, limit in (("objects", 12), ("clothing", 6)):
            assert isinstance(payload.get(field), list) and len(payload[field]) <= limit
            assert all(isinstance(v, str) and len(v) <= 30 for v in payload[field])
        assert set(payload) == {"caption", "activity", "objects", "clothing"}
    else:
        assert set(payload) == {"lines", "uncertain"} and isinstance(payload["uncertain"], bool)
        lines = payload["lines"]
        assert isinstance(lines, list) and len(lines) <= 6
        assert all(isinstance(v, str) and "\n" not in v for v in lines)
        assert sum(map(len, lines)) <= 180 and len(lines) == len(set(lines))
    return payload


def crop_image(path, region):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        box = tuple(round(v * (width if i % 2 == 0 else height)) for i, v in enumerate(region))
        image = image.crop(box)
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        return buffer.getvalue(), {
            "original_size": [width, height],
            "box": list(box),
            "size": list(image.size),
        }


def stage(identifier, name, path, output):
    directory = output / identifier / name
    data, metadata = crop_image(path, REGIONS[identifier][name])
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "image.jpg").write_bytes(data)
    save(
        directory / "crop.json",
        {**metadata, "source_sha256": sha(path), "crop_sha256": sha(directory / "image.jpg")},
    )
    description = name == "description"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": DESCRIPTION_PROMPT if description else OCR_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()},
                },
            ],
        }
    ]
    try:
        response = repair.completion(directory / "model", messages, json_mode=True)
        candidate = json.loads(response["choices"][0]["message"]["content"])
        try:
            validate_candidate(candidate, description=description)
        except (AssertionError, TypeError) as error:
            raise repair.ModelOutputError("Candidate violates bounded evidence schema") from error
        save(directory / "candidate.json", candidate)
        return {"id": identifier, "region": name, "status": "staged_for_visual_review"}
    except (repair.ModelOutputError, json.JSONDecodeError) as error:
        return {"id": identifier, "region": name, "status": "invalid_output", "error": str(error)}


def main():
    repair.assert_idle()
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    output = repair.CONTENT / "visual-review-v2"
    assert not output.exists(), "Inspect staged evidence before requesting again"
    plan = json.loads((repair.CONTENT / "plan.json").read_text())
    targets = {t["asset_id"]: t for t in plan["visual_targets"] if t["asset_id"] in REGIONS}
    assert set(targets) == set(REGIONS)
    with connect() as connection:
        assets = {
            identifier: dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
            for identifier in REGIONS
        }
    before = {}
    for identifier, target in targets.items():
        assert sha(assets[identifier]["path"]) == target["sha256"]
        current = repair.snapshot("observations", target["before"]["id"])
        success_path = repair.CONTENT / "visual" / identifier / "success.json"
        if success_path.exists():
            assert json.loads(success_path.read_text())["after"] == current
        else:
            assert current == target["before"]
        before[identifier] = current
    save(
        output / "intent.json",
        {
            "scope_id": repair.SCOPE,
            "model": repair.MODEL,
            "enable_thinking": False,
            "max_tokens": 4096,
            "max_workers": 4,
            "regions": REGIONS,
            "before": before,
            "writes_memories": False,
            "description_prompt": DESCRIPTION_PROMPT,
            "ocr_prompt": OCR_PROMPT,
            "plan_sha256": sha(repair.CONTENT / "plan.json"),
        },
    )
    progress = {"status": "running", "started_at": time.time(), "results": []}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(stage, identifier, name, assets[identifier]["path"], output)
                for identifier, regions in REGIONS.items()
                for name in regions
            ]
            for future in as_completed(futures):
                progress["results"].append(future.result())
                save(output / "progress.json", progress)
        for identifier, target in targets.items():
            assert repair.snapshot("observations", target["before"]["id"]) == before[identifier]
        progress["status"] = "staged_for_visual_review"
        progress["invalid_outputs"] = sum(r["status"] == "invalid_output" for r in progress["results"])
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(output / "terminal.json", progress)
        print(json.dumps(progress))
    return 1 if progress["invalid_outputs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
