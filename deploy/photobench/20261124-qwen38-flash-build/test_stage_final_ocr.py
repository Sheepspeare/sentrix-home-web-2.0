"""OCR staging preserves source pixels and accepts honest empty recognition."""

import io

import pytest
import stage_final_ocr as ocr
from PIL import Image


def test_rectification_keeps_source_file_and_enforces_exact_source_dimensions(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (20, 30), (20, 80, 140)).save(path)
    before = path.read_bytes()
    value = ocr.rectify(path, ((20, 30), (0, 0, 0, 30, 20, 30, 20, 0), (100, 150)))
    assert path.read_bytes() == before
    assert Image.open(io.BytesIO(value)).size == (100, 150)
    with pytest.raises(AssertionError):
        ocr.rectify(path, ((20, 31), (0, 0, 0, 30, 20, 30, 20, 0), (100, 150)))


def test_unreadable_region_can_return_no_text():
    assert ocr.validate({"lines": [], "uncertain": True})["lines"] == []


@pytest.mark.parametrize(
    "value",
    [
        {"lines": ["duplicated", "duplicated"], "uncertain": False},
        {"lines": ["invented\nsecond"], "uncertain": True},
        {"lines": ["x" * 501], "uncertain": True},
        {"lines": [str(x) for x in range(13)], "uncertain": False},
    ],
)
def test_invalid_or_unbounded_output_is_not_accepted(value):
    with pytest.raises(AssertionError):
        ocr.validate(value)
