"""Gemini → Digest (structured output): YouTube по ссылке, видео-файл через Files API, текст."""

import asyncio
import logging
import time
from pathlib import Path

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from digest.errors import DigestError
from digest.schema import Digest

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

# Встроенный ретрай SDK: лимиты free tier (429) и перегрузка (503).
RETRY_STATUS = [429, 503]
RETRY_ATTEMPTS = 3


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
        poll_interval_s: float = 2,
        processing_timeout_s: float = 180,
    ):
        self.api_key = api_key
        self.model = model
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

    async def _generate(self, parts: list[types.Part], what: str) -> Digest:
        contents = types.Content(role="user", parts=parts)
        try:
            resp = await self._get_client().aio.models.generate_content(
                model=self.model, contents=contents, config=self._config()
            )
        except errors.ClientError as err:
            if err.code == 429:
                raise DigestError(
                    "Лимит Gemini на сегодня исчерпан (free tier) — попробуй позже."
                ) from err
            raise DigestError(f"Gemini не смог открыть {what}: {err.message or err.code}") from err
        except errors.ServerError as err:
            raise DigestError("Gemini сейчас недоступен — попробуй через несколько минут.") from err
        return clean(self._parse(resp.text))

    async def digest_youtube(self, url: str, hint: str = "") -> Digest:
        """url — канонический https://www.youtube.com/watch?v=ID (публичное видео)."""
        # Part.from_uri не подходит: без mime_type он угадывает тип по расширению и падает.
        parts = [
            types.Part(file_data=types.FileData(file_uri=url)),
            types.Part(text=_with_hint("Сделай конспект этого видео.", hint)),
        ]
        return await self._generate(parts, "видео (unlisted/private или слишком длинное?)")

    async def digest_video_file(self, path: Path, mime_type: str, hint: str = "") -> Digest:
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
            return await self._generate(parts, "видео")
        finally:
            try:
                await files.delete(name=name)
            except Exception as err:  # Google и так удалит файл через 48 ч
                log.warning("Не смог удалить %s из Gemini Files: %s", name, err)

    async def _wait_active(self, f, name: str):
        deadline = time.monotonic() + self.processing_timeout_s
        while f.state == types.FileState.PROCESSING:
            if time.monotonic() >= deadline:
                raise DigestError("Gemini слишком долго обрабатывает видео — попробуй позже.")
            await asyncio.sleep(self.poll_interval_s)
            f = await self._get_client().aio.files.get(name=name)
        if f.state != types.FileState.ACTIVE:
            raise DigestError(f"Gemini не смог обработать видео (состояние {f.state}).")
        return f

    async def digest_text(self, text: str, title: str = "", hint: str = "") -> Digest:
        prompt = _with_hint("Сделай конспект этой статьи.", hint)
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
