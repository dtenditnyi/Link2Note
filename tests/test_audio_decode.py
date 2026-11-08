"""Интеграционный тест без моков: голосовое Telegram (ogg/opus) декодируется faster-whisper.

Ловит несовместимость версий PyAV и faster-whisper (например, убранный параметр av.open),
которую моки в test_voice.py скрыть могут, а живой бот — нет. Модель Whisper не грузится.
"""

from fractions import Fraction

import av
import numpy as np
from faster_whisper.audio import decode_audio

SAMPLE_RATE = 48_000  # Opus в Telegram — 48 кГц, моно


def make_voice_ogg(path, seconds: float = 1.0) -> None:
    """Синус 440 Гц → ogg/opus, как голосовое сообщение Telegram."""
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    pcm = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype(np.int16)
    with av.open(str(path), mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=SAMPLE_RATE, layout="mono")
        frame_size = 960  # 20 мс
        for i in range(0, len(pcm), frame_size):
            chunk = pcm[i : i + frame_size]
            if len(chunk) < frame_size:
                chunk = np.pad(chunk, (0, frame_size - len(chunk)))
            frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = SAMPLE_RATE
            frame.pts = i
            frame.time_base = Fraction(1, SAMPLE_RATE)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def test_telegram_voice_ogg_decodes(tmp_path):
    path = tmp_path / "voice.ogg"
    make_voice_ogg(path, seconds=1.0)

    audio = decode_audio(str(path), sampling_rate=16_000)

    assert audio.dtype == np.float32
    assert 15_000 <= len(audio) <= 17_000  # ~1 с при 16 кГц
    assert float(np.abs(audio).max()) > 0.05  # не тишина
