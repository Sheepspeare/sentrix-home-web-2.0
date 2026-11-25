"""Reviewed edits preserve unrelated content and read only selected model text."""

from types import SimpleNamespace

import apply_visual_review as apply

from sentrix.domain.media.models import ObservationFact


def test_street_correction_drops_false_fields_and_preserves_supported_content(monkeypatch):
    original = SimpleNamespace(
        caption="旧描述",
        activity="旧活动",
        event_type="街头售卖",
        place="原精确地址",
        objects=("南瓜", "不锈钢锅", "树木"),
        clothing=("黑色上衣",),
        source_text="原文保持",
        confidence=0.65,
        facts=(
            ObservationFact("摊位", "放置", "南瓜", 0.9),
            ObservationFact("招牌", "文字", "冰糖甜品", 1.0),
            ObservationFact("顾客", "穿着", "黑色外套", 0.95),
        ),
    )
    monkeypatch.setattr(
        apply,
        "response_payload",
        lambda *args: {
            "caption": "戴口罩的人站在摊位后，两名顾客在旁等候，背景可见楼房。",
            "activity": "人们停留",
        },
    )
    fixed = apply.corrected_semantics(original, apply.STREET)
    assert (
        fixed.ocr_text == "" and fixed.place == original.place and fixed.source_text == original.source_text
    )
    assert fixed.caption == "戴口罩的人站在摊位后，背景可见楼房。"
    assert fixed.objects == ("不锈钢锅", "树木")
    assert fixed.facts == (original.facts[2],) and fixed.clothing == original.clothing
    assert original.objects[0] == "南瓜"


def test_reviewed_ocr_extracts_exact_selected_lines_without_repairing_spelling(monkeypatch):
    lines = ["099", "wrong author", "Title from model", "wrong affiliation", "ECCV"]
    monkeypatch.setattr(apply, "response_payload", lambda *args: {"lines": lines})
    assert apply.reviewed_ocr("asset_75deb3249002") == "099\nTitle from model\nECCV"
    assert lines[1] == "wrong author"


def test_missing_caption_keeps_prior_fields_and_drops_only_reviewed_unsupported_fragment(monkeypatch):
    original = SimpleNamespace(
        caption="",
        activity="",
        event_type="",
        place="原精确地址",
        objects=(),
        clothing=(),
        facts=(),
        source_text="",
        confidence=0.65,
    )

    def payload(identifier, region):
        if region == "description":
            return {
                "caption": "墙面张贴纸张，地面可见部分杂物。",
                "activity": "展示",
                "objects": ["纸张", "杂物"],
                "clothing": [],
            }
        return {"lines": ["未选0", "未选1", "未选2", "原模型文字3", "原模型文字4"]}

    monkeypatch.setattr(apply, "response_payload", payload)
    fixed = apply.corrected_semantics(original, "asset_78a95b5d74d8")
    assert fixed.caption == "墙面张贴纸张。" and fixed.objects == ("纸张",)
    assert fixed.ocr_text == "原模型文字3\n原模型文字4" and fixed.place == original.place
