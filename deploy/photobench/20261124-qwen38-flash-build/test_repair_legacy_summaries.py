import pytest
import repair_legacy_summaries as legacy


def event(identifier, **values):
    return {
        "id": identifier,
        "scope_id": legacy.SCOPE,
        "source_type": "video_scene",
        "summary": "视频场景 · 0–2秒",
        **values,
    }


def test_select_only_scoped_placeholders():
    rows = [
        event("gap"),
        event("done", summary="一只狗在公园散步"),
        event("photo", source_type="photo"),
        event("other", scope_id="other"),
    ]
    assert legacy.select_targets(rows, {"done"}) == [rows[0]]


def test_excludes_successful_new_scene_set():
    with pytest.raises(AssertionError):
        legacy.select_targets([event("already")], {"already"})


def test_observation_drift_stops_before_model_or_write(monkeypatch, tmp_path):
    monkeypatch.setattr(legacy.repair, "CONTENT", tmp_path)
    monkeypatch.setattr(legacy.repair, "snapshot", lambda *args: {"id": "obs", "caption": "changed"})
    monkeypatch.setattr(legacy, "execute", lambda *args: pytest.fail("Must not execute"))
    with pytest.raises(AssertionError, match="Frozen input changed"):
        legacy.guarded_execute(event("gap"), [{"id": "obs", "caption": "original"}], None)


def test_changed_event_stops_before_request(monkeypatch, tmp_path):
    monkeypatch.setattr(legacy.repair, "CONTENT", tmp_path)
    monkeypatch.setattr(legacy.repair, "snapshot", lambda *args: event("gap", summary="already corrected"))
    monkeypatch.setattr(legacy, "execute", lambda *args: pytest.fail("Must not execute"))
    with pytest.raises(AssertionError, match="Frozen event changed"):
        legacy.guarded_execute(event("gap"), [], None)


def test_existing_success_is_reused_via_existing_verifier(monkeypatch, tmp_path):
    monkeypatch.setattr(legacy.repair, "CONTENT", tmp_path)
    directory = tmp_path / "summary/gap"
    directory.mkdir(parents=True)
    (directory / "success.json").write_text("{}")
    monkeypatch.setattr(
        legacy.repair, "snapshot", lambda *args: pytest.fail("Original event must not be required")
    )
    monkeypatch.setattr(
        legacy, "execute", lambda kind, before, repos: {"status": "reused", "id": before["id"]}
    )
    assert legacy.guarded_execute(event("gap"), [], None)["status"] == "reused"
