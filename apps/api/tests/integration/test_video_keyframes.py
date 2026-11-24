"""关键帧抽取：命令行形状、产物位置、失败判据（S8b T511）。

用假 subprocess：要钉的是「seek 用输入侧」「只取一帧」「产物落在哪」「失败了炸不炸」，
这些与机器上有没有 ffmpeg 无关（体例同 ``test_video_transcode.py``）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sentrix.infrastructure.media.video_keyframes import FfmpegVideoKeyframeExtractor

PATCH_TARGET = "sentrix.infrastructure.media.video_keyframes.subprocess.run"


def _extractor(tmp_path: Path) -> FfmpegVideoKeyframeExtractor:
    return FfmpegVideoKeyframeExtractor(tmp_path)


def test_the_target_is_the_scene_slot_under_the_derived_video_root(tmp_path: Path):
    source = tmp_path / "in.mov"
    source.write_bytes(b"x")

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"jpg")
        return Mock(returncode=0, stdout="", stderr="")

    with patch(PATCH_TARGET, side_effect=fake_run):
        result = _extractor(tmp_path).extract(source, "asset_1", 3, 42.5)

    assert result == tmp_path / "derived" / "video" / "asset_1" / "scenes" / "003" / "kf_0001.jpg"
    assert result.is_file()


def test_the_command_seeks_before_the_input_and_takes_exactly_one_frame(tmp_path: Path):
    source = tmp_path / "in.mov"
    source.write_bytes(b"x")

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"jpg")
        return Mock(returncode=0, stdout="", stderr="")

    with patch(PATCH_TARGET, side_effect=fake_run) as run:
        _extractor(tmp_path).extract(source, "asset_1", 0, 30.0)

    command = run.call_args.args[0]
    assert command[0] == "ffmpeg"
    # ``-ss`` 必须在 ``-i`` 之前：输入侧 seek。放到后面 ffmpeg 会整段解码到该秒，
    # 一个几小时的录像就是几十分钟的等待——命令仍然正确，只是慢得不可用。
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-ss") + 1] == "30.000000000"
    assert command[command.index("-frames:v") + 1] == "1"


def test_failure_raises_with_the_ffmpeg_stderr(tmp_path: Path):
    source = tmp_path / "in.mov"
    source.write_bytes(b"x")

    with (
        patch(PATCH_TARGET, return_value=Mock(returncode=1, stdout="", stderr="boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        _extractor(tmp_path).extract(source, "asset_1", 0, 0.0)


def test_a_zero_exit_without_the_output_file_is_still_a_failure(tmp_path: Path):
    """ffmpeg 退出 0 但产物不在，同样是失败——只看退出码会交出一个不存在的路径。"""
    source = tmp_path / "in.mov"
    source.write_bytes(b"x")

    with (
        patch(PATCH_TARGET, return_value=Mock(returncode=0, stdout="", stderr="")),
        pytest.raises(RuntimeError),
    ):
        _extractor(tmp_path).extract(source, "asset_1", 0, 0.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_the_last_frame_survives_a_timestamp_that_rounds_up_to_milliseconds(tmp_path: Path):
    source = tmp_path / "three-frames.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=30",
            "-frames:v",
            "3",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = _extractor(tmp_path).extract(source, "asset_last", 0, 2 / 30)
    assert result.stat().st_size > 0
