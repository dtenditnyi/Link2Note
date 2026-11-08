"""Расшифровка голосовых через faster-whisper.

Модель грузится лениво при первом голосовом (быстрый старт, тесты без модели),
а расшифровка идёт в отдельном потоке, чтобы не блокировать event loop бота.
"""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _default_factory(model_size: str, **kwargs: Any):
    from faster_whisper import WhisperModel  # тяжёлый импорт — только по требованию

    return WhisperModel(model_size, **kwargs)


class Transcriber:
    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        *,
        language: str = "ru",
        model_factory: Callable[..., Any] | None = None,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._factory = model_factory or _default_factory
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                self._model = self._factory(
                    self.model_size, device=self.device, compute_type=self.compute_type
                )
        return self._model

    def _transcribe_sync(self, path: Path) -> str:
        segments, _info = self._load().transcribe(
            str(path), beam_size=5, language=self.language, vad_filter=True
        )
        # segments — ленивый генератор: читаем его тоже в этом потоке.
        return "".join(s.text for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, path)
