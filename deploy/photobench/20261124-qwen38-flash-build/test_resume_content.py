"""A malformed answer must not replay prior calls or hide a configuration failure."""

import json
from types import SimpleNamespace

import httpx
import pytest
import resume_content as resume


def test_previous_incomplete_model_attempt_is_deferred_without_call(tmp_path, monkeypatch):
    monkeypatch.setattr(resume.repair, "CONTENT", tmp_path)
    path = tmp_path / "visual/asset/model-1/attempt-001"
    path.mkdir(parents=True)
    (path / "request.json").write_text("{}")
    monkeypatch.setattr(resume.repair, "repair_visual", lambda *_: pytest.fail("Repeated failed request"))
    result = resume.execute("visual", {"asset_id": "asset"}, None)
    assert result["status"] == "deferred_prior_model_failure"


def test_output_failure_retains_gap_and_next_item_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(resume.repair, "CONTENT", tmp_path)

    def repair(item, repos):
        if item["asset_id"] == "bad":
            raise AssertionError("Model output incomplete")
        return "completed"

    monkeypatch.setattr(resume.repair, "repair_visual", repair)
    assert resume.execute("visual", {"asset_id": "bad"}, None)["status"] == "failed_model_output"
    assert resume.execute("visual", {"asset_id": "good"}, None)["status"] == "completed"


def test_unexpected_failure_stops_instead_of_continuing(tmp_path, monkeypatch):
    monkeypatch.setattr(resume.repair, "CONTENT", tmp_path)
    monkeypatch.setattr(resume.repair, "repair_visual", lambda *_: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(AssertionError):
        resume.execute("visual", {"asset_id": "asset"}, None)


def test_summary_waits_for_caption_without_inventing_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(resume.repair, "CONTENT", tmp_path)
    repos = SimpleNamespace(
        events=SimpleNamespace(observation_ids=lambda _: ["obs"]),
        observations=SimpleNamespace(get=lambda _: SimpleNamespace(caption="")),
    )
    assert resume.execute("summary", {"id": "event"}, repos)["status"] == "deferred_missing_visual_evidence"


def test_successful_response_can_resume_index_without_model_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(resume.repair, "CONTENT", tmp_path)
    path = tmp_path / "visual/asset/model-1/attempt-001"
    path.mkdir(parents=True)
    (path / "request.json").write_text("{}")
    (path.parent / "accepted-response.json").write_text(json.dumps({"model": "qwen3.8-flash"}))
    monkeypatch.setattr(resume.repair, "repair_visual", lambda *_: "completed")
    assert resume.execute("visual", {"asset_id": "asset"}, None)["status"] == "completed"


@pytest.mark.parametrize(
    ("status", "message", "deferred"),
    [
        (
            400,
            "Model output became abnormal while generating a JSON response for response_format."
            " The generation was aborted because the partial output may be incomplete or invalid JSON.",
            True,
        ),
        (400, "Invalid max_tokens parameter", False),
        (401, "Authentication failed", False),
        (429, "Rate limit exceeded", False),
    ],
)
def test_only_confirmed_upstream_generation_error_is_isolated(
    tmp_path, monkeypatch, status, message, deferred
):
    monkeypatch.setattr(resume.repair, "CONTENT", tmp_path)
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "http://test"),
        json={"error": {"code": "invalid_parameter_error", "message": message}},
    )

    def fail(*_):
        response.raise_for_status()

    monkeypatch.setattr(resume.repair, "repair_visual", fail)
    if deferred:
        assert resume.execute("visual", {"asset_id": "asset"}, None)["status"] == "failed_model_output"
    else:
        with pytest.raises(httpx.HTTPStatusError):
            resume.execute("visual", {"asset_id": "asset"}, None)
