"""Explicit real-model gate: native startup and repeated HTTP recognition."""

import dataclasses
import wave
from unittest.mock import patch

import numpy as np
import pytest
import sherpa_onnx
from fastapi.testclient import TestClient

from sentrix.config import load_settings
from sentrix.infrastructure.models.sensevoice import SenseVoiceRecognizer


@pytest.fixture(scope="session")
def native_model_dir():
    return load_settings().speech_model_dir


@pytest.mark.real_environment
def test_native_startup_and_repeated_transcription(native_model_dir, isolated_app):
    model_dir = native_model_dir
    assert (model_dir / "model.int8.onnx").is_file(), "run make speech-prepare first"
    with wave.open(str(model_dir / "test_wavs/zh.wav")) as audio:
        assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
        rate = audio.getframerate()
        pcm = (
            np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype("<f4") / 32768
        ).tobytes()
    service = SenseVoiceRecognizer(model_dir)
    isolated_app.state.container = dataclasses.replace(
        isolated_app.state.container,
        speech_recognizer=service,
    )
    with patch.object(
        sherpa_onnx.OfflineRecognizer,
        "from_sense_voice",
        wraps=sherpa_onnx.OfflineRecognizer.from_sense_voice,
    ) as factory:
        with TestClient(isolated_app) as client:
            assert client.get("/api/v1/assistant/speech/status").json()["available"]
            results = []
            for _ in range(2):
                response = client.post(
                    f"/api/v1/assistant/speech/transcribe?sample_rate={rate}",
                    content=pcm,
                    headers={"Content-Type": "application/octet-stream"},
                )
                assert response.status_code == 200
                results.append(response.json()["text"])
            assert results[0] and results[0] == results[1]
            assert any("\u4e00" <= char <= "\u9fff" for char in results[0])
            factory.assert_called_once()
            print(f"Real SenseVoice result: {results[0]}")
        assert not service.available
