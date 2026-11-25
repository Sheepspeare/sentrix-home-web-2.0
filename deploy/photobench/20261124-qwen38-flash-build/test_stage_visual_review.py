"""Bounded OCR staging must preserve media and avoid publishing a memory."""

import io
import json

import pytest
import stage_visual_review as stage
from PIL import Image


@pytest.mark.parametrize("lines", [["重复"] * 2, ["x"] * 7, ["x" * 181], ["第一行\n第二行"]])
def test_rejects_unbounded_or_duplicate_ocr(lines):
    with pytest.raises(AssertionError):
        stage.validate_candidate({"lines": lines, "uncertain": False}, description=False)


def test_crop_preserves_original_and_uses_recorded_coordinates(tmp_path):
    path = tmp_path / "source.jpg"
    Image.new("RGB", (200, 100), "red").save(path)
    before = path.read_bytes()
    cropped, metadata = stage.crop_image(path, (0.25, 0.2, 0.75, 0.8))
    assert path.read_bytes() == before
    assert metadata == {"original_size": [200, 100], "box": [50, 20, 150, 80], "size": [100, 60]}
    assert Image.open(io.BytesIO(cropped)).size == (100, 60)


def test_stage_saves_candidate_only_with_image_and_request_evidence(tmp_path, monkeypatch):
    path = tmp_path / "source.jpg"
    Image.new("RGB", (100, 100), "white").save(path)
    before = path.read_bytes()
    calls = []

    def complete(directory, messages, *, json_mode):
        calls.append((directory, messages, json_mode))
        return {"choices": [{"message": {"content": '{"lines":["文字"],"uncertain":true}'}}]}

    monkeypatch.setattr(stage.repair, "completion", complete)
    monkeypatch.setattr(
        stage.repair, "repositories", lambda: pytest.fail("Staging must not open repositories")
    )
    output = tmp_path / "staged"
    result = stage.stage("asset_816c96df9415", "sign", path, output)
    directory = output / "asset_816c96df9415" / "sign"
    assert result["status"] == "staged_for_visual_review"
    assert json.loads((directory / "candidate.json").read_text())["lines"] == ["文字"]
    assert calls[0][2] is True
    assert calls[0][1][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert path.read_bytes() == before and not list(tmp_path.rglob("success.json"))
