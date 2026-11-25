"""Read only the English dish names in the unresolved first video frame."""

import io
import json

import repair_content as repair
import stage_final_ocr as stage
from PIL import Image
from recover_videos import save, sha

OUTPUT = stage.OUTPUT / "menu-detail"
ASSET = "asset_ad16dcecda08"
BOX = (280, 260, 850, 590)
PROMPT = (
    "逐字转录图片中能够辨认的英文菜名。只读英文，不读中文、编号、价格或残缺标题。"
    "不要翻译，不按菜单常识补全；不清楚的整行略过。最多4行。"
    "返回JSON：lines（英文字符串数组），uncertain（布尔值）。"
)


def crop(data):
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (880, 600)
        result = image.crop(BOX).resize((1140, 660), Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    result.save(buffer, "PNG")
    return buffer.getvalue()


def main():
    repair.assert_idle()
    assert not OUTPUT.exists(), "Inspect the saved detail response before continuing"
    intent = json.loads((stage.OUTPUT / "intent.json").read_text())
    before = intent["before"][ASSET]
    assert repair.snapshot("observations", before["id"]) == before
    source = stage.OUTPUT / ASSET / "image.png"
    data = crop(source.read_bytes())
    OUTPUT.mkdir()
    (OUTPUT / "image.png").write_bytes(data)
    save(OUTPUT / "intent.json", {
        "asset_id": ASSET, "box": BOX, "source_image_sha256": sha(source),
        "image_sha256": sha(OUTPUT / "image.png"), "prompt": PROMPT,
        "new_requests": 1, "writes_memories": False,
    })
    stage.PROMPT = PROMPT
    result = stage.candidate(ASSET, OUTPUT, data)
    assert repair.snapshot("observations", before["id"]) == before
    save(OUTPUT / "terminal.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
