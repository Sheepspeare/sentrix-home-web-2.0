"""Sampling retains decoded timestamps, including the exact final frame."""

from pathlib import Path

import cv2
import numpy as np

from sentrix.infrastructure.media.svd_katna_scene_planner import _sample_video


def test_variable_frame_times_come_from_the_decoder(monkeypatch):
    times = [0.0, 0.1, 0.14]

    class Capture:
        index = -1

        def isOpened(self):
            return True

        def get(self, key):
            return {
                cv2.CAP_PROP_FPS: 30.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64,
                cv2.CAP_PROP_FRAME_HEIGHT: 48,
                cv2.CAP_PROP_POS_MSEC: times[self.index] * 1000,
            }[key]

        def read(self):
            self.index += 1
            return (True, np.zeros((48, 64, 3), dtype=np.uint8)) if self.index < len(times) else (False, None)

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda _: Capture())
    frames, sampling, _ = _sample_video(Path("variable.mp4"), 0.18)
    assert [timestamp for timestamp, _ in frames] == times
    assert sampling["source_last_frame_timestamp_sec"] == 0.14
    assert sampling["tail_anchor_added"] is True
    assert sampling["timestamp_basis"] == "decoded_frame_pts"
