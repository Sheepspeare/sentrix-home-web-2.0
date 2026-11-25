"""Read three source images through rectified text crops; stage model evidence only."""

import base64
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import repair_content as repair
from PIL import Image, ImageOps
from recover_videos import connect, save, sha

OUTPUT = repair.CONTENT / "ocr-final-targeted"
# PIL QUAD order: upper left, lower left, lower right, upper right.
REGIONS = {
    "asset_ad16dcecda08": ((360, 480), (63, 367, 88, 479, 265, 434, 228, 362), (880, 600)),
    "asset_042fdf27e328": ((360, 480), (73, 369, 92, 478, 254, 436, 220, 366), (880, 600)),
    "asset_816c96df9415": ((750, 1000), (186, 678, 191, 871, 414, 796, 414, 638), (1000, 850)),
}
PROMPT = (
    "这是从原始照片的文字区域按透视几何校正、插值放大的图，没有增加新的文字细节。"
    "只逐字转录图中确实能辨认的印刷文字；保留原语言，最多12行、合计500字。"
    "模糊、被遮挡或裁掉的文字不要补全，不根据菜名常识猜字，不根据食物推断菜单。"
    "数字和价格只有清楚可辨时才抄，不生成连续编号或延长价格序列。"
    "短的可靠片段也可以；不能可靠读取就返回空lines。"
    "只返回JSON对象：lines（字符串数组，每项一行），uncertain（布尔值）。"
)


def rectify(path, specification):
    expected_size, quad, size = specification
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        assert image.size == expected_size
        result = image.transform(size, Image.Transform.QUAD, quad, Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    result.save(buffer, "PNG")
    return buffer.getvalue()


def validate(value):
    assert isinstance(value, dict) and set(value) == {"lines", "uncertain"}
    lines = value["lines"]
    assert isinstance(value["uncertain"], bool) and isinstance(lines, list) and len(lines) <= 12
    assert all(isinstance(line, str) and line.strip() and "\n" not in line for line in lines)
    assert sum(map(len, lines)) <= 500 and len(lines) == len(set(lines))
    return value


def candidate(identifier, directory, data):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()},
                },
            ],
        }
    ]
    response = repair.completion(directory / "model", messages, json_mode=True)
    value = json.loads(response["choices"][0]["message"]["content"])
    validate(value)
    save(directory / "candidate.json", value)
    return {"asset_id": identifier, "lines": len(value["lines"]), "status": "pending_visual_review"}


def main():
    repair.assert_idle()
    assert not OUTPUT.exists(), "Inspect saved candidates before requesting more OCR"
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    with connect() as connection:
        assets = {
            identifier: dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
            for identifier in REGIONS
        }
        before = {
            identifier: dict(
                connection.execute("SELECT * FROM observations WHERE asset_id=?", (identifier,)).fetchone()
            )
            for identifier in REGIONS
        }
    original_plan = json.loads((repair.CONTENT / "plan.json").read_text())
    frozen = {t["asset_id"]: t for t in original_plan["visual_targets"] if t["asset_id"] in REGIONS}
    images = {}
    for identifier in REGIONS:
        assert before[identifier]["scope_id"] == repair.SCOPE and not before[identifier]["ocr_text"]
        assert sha(assets[identifier]["path"]) == frozen[identifier]["sha256"]
        images[identifier] = rectify(assets[identifier]["path"], REGIONS[identifier])
        directory = OUTPUT / identifier
        directory.mkdir(parents=True)
        (directory / "image.png").write_bytes(images[identifier])
        save(
            directory / "image-metadata.json",
            {
                "source_sha256": frozen[identifier]["sha256"],
                "rectification": REGIONS[identifier],
                "image_sha256": sha(directory / "image.png"),
            },
        )
    save(
        OUTPUT / "intent.json",
        {
            "scope_id": repair.SCOPE,
            "before": before,
            "assets": assets,
            "model": repair.MODEL,
            "enable_thinking": False,
            "max_tokens": 4096,
            "workers": 3,
            "new_requests_planned": 3,
            "prompt": PROMPT,
            "writes_memories": False,
            "source_script_sha256": sha(__file__),
        },
    )
    progress = {"status": "running", "started_at": time.time(), "results": []}
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(candidate, identifier, OUTPUT / identifier, data): identifier
                for identifier, data in images.items()
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as error:
                    result = {
                        "asset_id": futures[future],
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                progress["results"].append(result)
                save(OUTPUT / "progress.json", progress)
        for identifier, row in before.items():
            assert repair.snapshot("observations", row["id"]) == row
            assert sha(assets[identifier]["path"]) == frozen[identifier]["sha256"]
        progress["status"] = "staged_for_visual_review"
        progress["failed"] = sum(r["status"] == "failed" for r in progress["results"])
    except Exception as error:
        progress.update(status="failed", error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(OUTPUT / "terminal.json", progress)
        print(json.dumps(progress))
    return 1 if progress["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
