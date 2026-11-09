"""Ссылка → (Digest, Markdown). Общий путь для Telegram-воркера и CLI, без записи в хранилище."""

from datetime import date
from typing import Protocol

import httpx

from digest.classifier import Source, canonical_youtube, classify, youtube_id
from digest.errors import DigestError
from digest.fetchers import youtube_meta
from digest.render import to_markdown
from digest.schema import Digest


class DigestModel(Protocol):
    async def digest_youtube(self, url: str, hint: str = "") -> Digest: ...


async def make_note(
    url: str, hint: str, *, llm: DigestModel, http: httpx.AsyncClient, today: date
) -> tuple[Digest, str]:
    vid = youtube_id(url)
    if vid is None:
        if classify(url) is Source.YOUTUBE:
            raise DigestError(
                "Это не ссылка на видео (канал или плейлист?) — пришли ссылку на ролик."
            )
        raise DigestError("Пока умею только YouTube — статьи, Reels и TikTok появятся на этапе 2.")
    canonical = canonical_youtube(vid)
    meta = await youtube_meta(canonical, http)  # приватное/удалённое → DigestError до Gemini
    d = await llm.digest_youtube(canonical, hint)
    md = to_markdown(
        d, url=canonical, source=Source.YOUTUBE.value, author=meta.author, date_added=today
    )
    return d, md
