"""Gemini → Digest (structured output): YouTube по ссылке, видео-файл через Files API, текст."""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from digest.errors import DigestError, GeminiBusy, VideoUnreadable
from digest.schema import Digest
from services.gemini_errors import is_daily_quota, is_overloaded

log = logging.getLogger(__name__)

# Смысл тегов — прямо в промпте: без описаний модель ставила мультфильму books/games.
TAG_MEANINGS = {
    "dev": "программирование, IDE, git, DevOps, инструменты разработчика",
    "ai": "нейросети, LLM, машинное обучение, AI-инструменты",
    "homelab": "домашний сервер, Proxmox, Docker, сеть, self-hosted",
    "university": "учёба в университете: предметы, лабы, экзамены",
    "romanian": "румынский язык",
    "nuclear": "ядерная физика и энергетика",
    "driving": "вождение, ПДД, автомобили",
    "cooking": "рецепты и кулинария",
    "sport": "тренировки и спорт",
    "health": "здоровье, сон, питание",
    "finance": "деньги, бюджет, инвестиции",
    "planning": "продуктивность, планирование, тайм-менеджмент",
    "games": "видеоигры и геймдев (не мультфильмы и не кино)",
    "books": "только если материал про конкретную книгу или книги (обзор, пересказ, подборка)",
    "personal": "саморазвитие и личное; если ничего другого не подходит",
    "obsidian": "Obsidian, заметки, «второй мозг»",
    "fun": "юмор, мультфильмы, кино, развлечения, просто интересное",
}
ALLOWED_TAGS = list(TAG_MEANINGS)
MAX_TAGS = 3
FALLBACK_TAG = "personal"
MAX_TEXT_CHARS = 200_000

_TAG_LINES = "\n".join(f"  - {tag} — {meaning}" for tag, meaning in TAG_MEANINGS.items())

SYSTEM = f"""Ты — мой ассистент по конспектам. Сделай из материала (видео или статьи) \
заметку для Obsidian на русском.
Правила:
- Пиши только то, что реально есть в материале. Не выдумывай. Если не уверен — confidence=low.
- Горячие клавиши, команды, названия программ и книг переписывай ТОЧНО, как в материале \
(в видео смотри и на экран, и на звук).
- Технические термины оставляй на английском.
- steps — только если в материале есть процесс/инструкция.
- action_items — 1–3 конкретных действия для меня \
(студент Information Engineering, Python/Java, homelab, Obsidian).
- timestamps — секунды от начала видео, только реальные моменты; для статьи — пустой список.
- tags — 1–3 тега по главной теме материала, только из списка:
{_TAG_LINES}
"""

TRANSCRIPT_PROMPT = (
    "Сделай конспект видео по расшифровке его звука. Экрана ты не видишь: горячие клавиши, "
    "команды и код пиши, только если их проговорили. Строки начинаются с меток [MM:SS] "
    "(или [H:MM:SS]) — время от начала видео; по ним заполни timestamps (в секундах)."
)

# Встроенный ретрай SDK: лимиты free tier (429) и перегрузка (503).
RETRY_STATUS = [429, 503]  # 500/502/504 часто постоянные — не множим запросы
RETRY_ATTEMPTS = 3
# Настоящая перегрузка — задачу стоит отложить и повторить позже.
POSTPONE_CODES = (429, 503)


@dataclass(frozen=True)
class Digested:
    """Конспект и модель, которая его сделала (основная или запасная)."""

    digest: Digest
    model: str


def clean(d: Digest) -> Digest:
    """Теги — только из списка, без повторов, максимум 3; read_minutes не меньше 1."""
    tags: list[str] = []
    for raw in d.tags:
        tag = raw.strip().lower()
        if tag in ALLOWED_TAGS and tag not in tags:
            tags.append(tag)
    d.tags = tags[:MAX_TAGS] or [FALLBACK_TAG]
    d.read_minutes = max(1, d.read_minutes)
    return d


def _with_hint(prompt: str, hint: str) -> str:
    if hint.strip():
        prompt += f"\nПожелание пользователя: {hint.strip()}"
    return prompt


class DigestLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_s: int = 300,
        client=None,
        *,
        fallback_model: str = "",
        poll_interval_s: float = 2,
        processing_timeout_s: float = 180,
    ):
        self.api_key = api_key
        self.model = model
        # Запасная = основная — это та же модель, вторая попытка ничего не даст.
        self.fallback_model = fallback_model if fallback_model != model else ""
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.processing_timeout_s = processing_timeout_s
        self._client = client

    def _get_client(self):
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Digest,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,  # ~100 токенов/с видео
            temperature=0.2,
            http_options=types.HttpOptions(
                timeout=self.timeout_s * 1000,  # миллисекунды
                retry_options=types.HttpRetryOptions(
                    attempts=RETRY_ATTEMPTS, http_status_codes=RETRY_STATUS
                ),
            ),
        )

    async def _generate(
        self, parts: list[types.Part], what: str, *, video: bool = False
    ) -> Digested:
        """Основная модель (с повторами SDK); при 429/5xx — одна попытка на запасной.

        video=True: ошибка «не открыл» (400/403/404) — VideoUnreadable, есть путь через Whisper.
        """
        contents = types.Content(role="user", parts=parts)
        models = [self.model] + ([self.fallback_model] if self.fallback_model else [])
        failures: list[errors.APIError] = []
        for model in models:
            try:
                resp = await self._get_client().aio.models.generate_content(
                    model=model, contents=contents, config=self._config()
                )
            except errors.APIError as err:
                if not is_overloaded(err):
                    error = VideoUnreadable if video else DigestError
                    raise error(
                        f"Gemini не смог открыть {what}: {err.message or err.code}"
                    ) from err
                failures.append(err)
                if model != models[-1]:
                    log.warning("Gemini %s: %s → запасная %s", model, err.code, models[-1])
                continue
            log.info("Конспект сделала модель %s", model)
            return Digested(clean(self._parse(resp.text)), model)

        last = failures[-1]
        if not any(err.code in POSTPONE_CODES for err in failures):
            # 500/502/504 у обеих моделей: часто постоянная ошибка (кодек, длина) — не откладываем.
            raise DigestError(
                f"Gemini сейчас недоступен (ошибка {last.code}) — попробуй позже."
            ) from last
        daily = all(is_daily_quota(err) for err in failures)
        if daily:
            message = "Лимит Gemini на сегодня исчерпан (free tier)."
        elif last.code == 429:
            message = "Лимит запросов к Gemini исчерпан — попробую позже."
        else:
            message = "Gemini сейчас недоступен (перегружен) — попробую позже."
        raise GeminiBusy(message, daily=daily) from last

    async def digest_youtube(self, url: str, hint: str = "") -> Digested:
        """url — канонический https://www.youtube.com/watch?v=ID (публичное видео)."""
        # Part.from_uri не подходит: без mime_type он угадывает тип по расширению и падает.
        parts = [
            types.Part(file_data=types.FileData(file_uri=url)),
            types.Part(text=_with_hint("Сделай конспект этого видео.", hint)),
        ]
        return await self._generate(
            parts, "видео (unlisted/private или слишком длинное?)", video=True
        )

    async def digest_video_file(self, path: Path, mime_type: str, hint: str = "") -> Digested:
        """Видео-файл → Files API → конспект. Файл в облаке удаляется в любом случае."""
        files = self._get_client().aio.files
        try:
            f = await files.upload(file=str(path), config={"mime_type": mime_type})
        except errors.APIError as err:
            raise DigestError(
                f"Не смог загрузить видео в Gemini: {err.message or err.code}"
            ) from err
        name = f.name or ""
        try:
            f = await self._wait_active(f, name)
            parts = [
                types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type)),
                types.Part(text=_with_hint("Сделай конспект этого видео.", hint)),
            ]
            return await self._generate(parts, "видео", video=True)
        finally:
            try:
                await files.delete(name=name)
            except Exception as err:  # Google и так удалит файл через 48 ч
                log.warning("Не смог удалить %s из Gemini Files: %s", name, err)

    async def _wait_active(self, f, name: str):
        deadline = time.monotonic() + self.processing_timeout_s
        while f.state == types.FileState.PROCESSING:
            if time.monotonic() >= deadline:
                raise VideoUnreadable("Gemini слишком долго обрабатывает видео — попробуй позже.")
            await asyncio.sleep(self.poll_interval_s)
            f = await self._get_client().aio.files.get(name=name)
        if f.state != types.FileState.ACTIVE:
            raise VideoUnreadable(f"Gemini не смог обработать видео (состояние {f.state}).")
        return f

    async def digest_text(
        self, text: str, title: str = "", hint: str = "", *, transcript: bool = False
    ) -> Digested:
        """transcript=True — текст это расшифровка звука видео (Whisper) со строками [MM:SS]."""
        prompt = _with_hint(
            TRANSCRIPT_PROMPT if transcript else "Сделай конспект этой статьи.", hint
        )
        if title.strip():
            prompt += f"\nЗаголовок: {title.strip()}"
        prompt += f"\n\n{text[:MAX_TEXT_CHARS]}"
        return await self._generate([types.Part(text=prompt)], "статью")

    @staticmethod
    def _parse(text: str | None) -> Digest:
        # resp.parsed молча глотает ValidationError — разбираем сами, чтобы дать понятную ошибку.
        if not text:
            raise DigestError("ИИ вернул пустой ответ. Попробуй ещё раз позже.")
        try:
            return Digest.model_validate_json(text)
        except ValidationError as err:
            raise DigestError("ИИ вернул конспект в неверном формате. Попробуй ещё раз.") from err
