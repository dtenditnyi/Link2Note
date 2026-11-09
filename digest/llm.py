"""Gemini: YouTube-видео по ссылке → Digest (structured output, без скачивания видео)."""

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from digest.errors import DigestError
from digest.schema import Digest

ALLOWED_TAGS = [
    "dev",
    "ai",
    "homelab",
    "university",
    "romanian",
    "nuclear",
    "driving",
    "cooking",
    "sport",
    "health",
    "finance",
    "planning",
    "games",
    "books",
    "personal",
    "obsidian",
]
MAX_TAGS = 3
FALLBACK_TAG = "personal"

SYSTEM = f"""Ты — мой ассистент по конспектам. Сделай из материала заметку для Obsidian на русском.
Правила:
- Пиши только то, что реально есть в материале. Не выдумывай. Если не уверен — confidence=low.
- Горячие клавиши, команды, названия программ и книг переписывай ТОЧНО, как в видео \
(смотри и на экран, и на звук).
- Технические термины оставляй на английском.
- steps — только если в материале есть процесс/инструкция.
- action_items — 1–3 конкретных действия для меня \
(студент Information Engineering, Python/Java, homelab, Obsidian).
- timestamps — секунды от начала видео, только реальные моменты.
- tags — 1–3 из списка: {", ".join(ALLOWED_TAGS)}.
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


class DigestLLM:
    def __init__(self, api_key: str, model: str, timeout_s: int = 300, client=None):
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
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

    async def digest_youtube(self, url: str, hint: str = "") -> Digest:
        """url — канонический https://www.youtube.com/watch?v=ID (публичное видео)."""
        prompt = "Сделай конспект этого видео."
        if hint.strip():
            prompt += f"\nПожелание пользователя: {hint.strip()}"
        # Part.from_uri не подходит: без mime_type он угадывает тип по расширению и падает.
        contents = types.Content(
            role="user",
            parts=[
                types.Part(file_data=types.FileData(file_uri=url)),
                types.Part(text=prompt),
            ],
        )
        try:
            resp = await self._get_client().aio.models.generate_content(
                model=self.model, contents=contents, config=self._config()
            )
        except errors.ClientError as err:
            if err.code == 429:
                raise DigestError(
                    "Лимит Gemini на сегодня исчерпан (free tier) — попробуй позже."
                ) from err
            raise DigestError(
                f"Gemini не смог открыть видео (unlisted/private или слишком длинное?): "
                f"{err.message or err.code}"
            ) from err
        except errors.ServerError as err:
            raise DigestError("Gemini сейчас недоступен — попробуй через несколько минут.") from err
        return clean(self._parse(resp.text))

    @staticmethod
    def _parse(text: str | None) -> Digest:
        # resp.parsed молча глотает ValidationError — разбираем сами, чтобы дать понятную ошибку.
        if not text:
            raise DigestError("ИИ вернул пустой ответ. Попробуй ещё раз позже.")
        try:
            return Digest.model_validate_json(text)
        except ValidationError as err:
            raise DigestError("ИИ вернул конспект в неверном формате. Попробуй ещё раз.") from err
