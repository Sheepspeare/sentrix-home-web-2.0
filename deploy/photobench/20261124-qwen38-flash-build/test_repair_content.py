"""Content recovery retains responses across index failure and limits field writes."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import repair_content as repair
from PIL import Image
from settle_content_smoke import UNSUPPORTED_CAPTION, corrected_semantics

from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation
from sentrix.domain.memories.events import EventRecord


def response(content=None, *, finish="stop", model=repair.MODEL, reasoning=""):
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": json.dumps(
                        content or {"title": "场景", "summary": "餐桌上的食物"}, ensure_ascii=False
                    ),
                    "reasoning_content": reasoning,
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"finish": "length"},
        {"model": "other-model"},
        {"reasoning": "thinking"},
        {"content": {"title": "待总结事件", "summary": "占位"}},
    ],
)
def test_invalid_generation_remains_failed_and_retry_is_recorded(tmp_path, monkeypatch, change):
    replies = [response(**change), response()]
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", "http://test"), json=replies.pop(0))

    monkeypatch.setattr(repair.httpx, "post", post)
    with pytest.raises(AssertionError):
        repair.completion(tmp_path, [{"role": "user", "content": "evidence"}])
    assert not (tmp_path / "accepted-response.json").exists()
    repair.completion(tmp_path, [{"role": "user", "content": "evidence"}])
    repair.completion(tmp_path, [{"role": "user", "content": "evidence"}])
    assert len(calls) == 2
    assert (tmp_path / "attempt-001/response.json").exists()
    assert (tmp_path / "attempt-002/response.json").exists()
    assert all(c["max_tokens"] == 4096 and c["enable_thinking"] is False for c in calls)


def test_missing_semantics_excludes_partial_success():
    assert repair.missing_semantics({"place": "precise address"})
    for value in ({"activity": "在吃饭"}, {"objects_json": '["餐盘"]'}, {"ocr_text": "菜单"}):
        assert not repair.missing_semantics(value)


def test_review_removes_only_disproven_text_and_preserves_other_visual_fields():
    observation = SimpleNamespace(
        caption="桌上有菜。" + UNSUPPORTED_CAPTION,
        place="原地点",
        activity="用餐",
        event_type="餐饮",
        objects=("铁板",),
        clothing=(),
        facts=(),
        source_text="",
        confidence=0.65,
    )
    semantics = corrected_semantics(observation, repair.EMPTY_SMOKE)
    assert semantics.caption == "桌上有菜。盘子下方垫着菜单纸。"
    assert semantics.ocr_text == ""
    assert semantics.objects == observation.objects and semantics.place == observation.place
    assert corrected_semantics(observation, repair.OCR_REVIEW).caption == observation.caption


def test_summary_resume_after_embedding_failure_does_not_repeat_model_or_apply(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    event = EventRecord(
        id="evt_fixture",
        scope_id=repair.SCOPE,
        title="视频场景 1",
        event_type="",
        time_start=now,
        time_end=now,
        place="餐馆",
        admin_district="",
        activity="",
        summary="视频场景 · 0~1",
        confidence=0.5,
        aggregation_score=0,
        aggregation_breakdown={},
        created_at=now,
        source_type="video_scene",
    )
    observation = SimpleNamespace(
        id="obs", caption="桌上有一盘菜", activity="用餐", place="餐馆", objects=(), clothing=(), ocr_text=""
    )
    before = {"id": event.id, "revision": 1, "title": event.title, "summary": event.summary}
    state = dict(before)
    applied = []
    model_calls = []
    vectors = []

    def apply(identifier, **kwargs):
        nonlocal event
        applied.append(identifier)
        event = replace(event, **kwargs, revision=event.revision + 1)
        state.update(revision=2, title=event.title, summary=event.summary)
        return event

    embeddings = [[], [0.1, 0.2]]
    repos = SimpleNamespace(
        events=SimpleNamespace(get=lambda _: event, observation_ids=lambda _: ["obs"], apply_summary=apply),
        observations=SimpleNamespace(get=lambda _: observation),
        participants=SimpleNamespace(list_participants=lambda _: []),
        embedder=SimpleNamespace(model_id="test", dimension=2, embed_query=lambda _: embeddings.pop(0)),
        vectors=SimpleNamespace(upsert=vectors.append),
    )

    def post(url, **kwargs):
        model_calls.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", "http://test"), json=response())

    monkeypatch.setattr(repair, "CONTENT", tmp_path)
    monkeypatch.setattr(repair, "snapshot", lambda *_: dict(state))
    monkeypatch.setattr(repair.httpx, "post", post)
    with pytest.raises(AssertionError, match="embedding"):
        repair.repair_summary(before, repos)
    assert repair.repair_summary(before, repos) == "completed"
    assert repair.repair_summary(before, repos) == "reused"
    assert len(model_calls) == len(applied) == len(vectors) == 1
    assert vectors[0].source_id == event.id and vectors[0].metadata == {"observation_id": "obs"}


def test_ocr_repair_preserves_successful_visual_fields_and_refreshes_indexes(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (8, 8), "white").save(image)
    asset = Asset(
        id=repair.OCR_REVIEW,
        scope_id=repair.SCOPE,
        file_name="frame.jpg",
        media_type=MediaType.IMAGE,
        path=str(image),
        mime_type="image/jpeg",
        size_bytes=image.stat().st_size,
        status=AssetStatus.PROCESSED,
        content_sha256=repair.sha(image),
        captured_at=now,
        captured_location=None,
        created_at=now,
        updated_at=now,
    )
    original = Observation(
        id="obs_fixture",
        scope_id=repair.SCOPE,
        asset_id=asset.id,
        source_type="import",
        captured_at=now,
        confidence=0.65,
        created_at=now,
        updated_at=now,
        caption="原有成功描述",
        activity="用餐",
        place="精确地点",
        ocr_text="虚构价序列",
        objects=("餐盘",),
    )
    before = {"id": original.id, "revision": 1, "caption": original.caption, "ocr_text": original.ocr_text}
    current = original
    state = dict(before)
    writes = []
    embedded = []
    lexical = []

    def enrich(identifier, *, semantics):
        nonlocal current
        writes.append(semantics)
        current = replace(
            current,
            **{k: getattr(semantics, k) for k in ("caption", "activity", "place", "ocr_text", "objects")},
            revision=2,
        )
        state.update(revision=2, caption=current.caption, ocr_text=current.ocr_text)
        return current

    repos = SimpleNamespace(
        assets=SimpleNamespace(get=lambda _: asset),
        observations=SimpleNamespace(get=lambda _: current, enrich=enrich),
        embedder=SimpleNamespace(
            model_id="test", dimension=2, embed_query=lambda t: embedded.append(t) or [1.0, 0.0]
        ),
        vectors=SimpleNamespace(upsert=lambda _: None),
        lexical=SimpleNamespace(refresh_from_observation=lambda o: lexical.append(o) or 3),
    )
    monkeypatch.setattr(repair, "CONTENT", tmp_path / "content")
    monkeypatch.setattr(repair, "snapshot", lambda *_: dict(state))
    monkeypatch.setattr(
        repair.httpx,
        "post",
        lambda *a, **k: httpx.Response(
            200,
            request=httpx.Request("POST", "http://test"),
            json=response(
                {
                    "caption": "新的描述",
                    "activity": "摆拍",
                    "place": "新地点",
                    "ocr_text": "看不清",
                    "objects": ["新物体"],
                }
            ),
        ),
    )
    target = {
        "asset_id": asset.id,
        "mode": "ocr_only",
        "before": before,
        "sha256": repair.sha(image),
        "event_ids": ["evt"],
    }
    assert repair.repair_visual(target, repos) == "completed"
    assert repair.repair_visual(target, repos) == "reused"
    assert len(writes) == len(embedded) == len(lexical) == 1
    assert current.caption == original.caption and current.objects == original.objects
    assert current.place == original.place and current.ocr_text == "看不清"
    assert "虚构价序列" not in embedded[0] and "看不清" in embedded[0]
