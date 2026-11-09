"""Метаданные видео без ключей API: YouTube oEmbed (автор, название).

Заодно ранняя проверка доступности: приватное или удалённое видео отсекаем до Gemini,
не тратя квоту. Любые другие сбои oEmbed не мешают конспекту — просто без автора.
"""

import logging
from dataclasses import dataclass

import httpx

from digest.errors import DigestError

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"


@dataclass(frozen=True)
class VideoMeta:
    title: str = ""
    author: str = ""


async def youtube_meta(url: str, client: httpx.AsyncClient) -> VideoMeta:
    try:
        resp = await client.get(OEMBED_URL, params={"url": url, "format": "json"})
    except httpx.HTTPError as err:
        log.warning("oEmbed недоступен для %s: %s", url, err)
        return VideoMeta()

    if resp.status_code in (401, 403):
        raise DigestError(
            "Видео приватное или с ограничениями — Gemini смотрит только публичные видео."
        )
    if resp.status_code == 404:
        raise DigestError("Видео не найдено — возможно, его удалили.")
    if resp.status_code != 200:
        log.warning("oEmbed вернул %s для %s", resp.status_code, url)
        return VideoMeta()

    try:
        data = resp.json()
    except ValueError:
        log.warning("oEmbed вернул не JSON для %s", url)
        return VideoMeta()
    return VideoMeta(title=str(data.get("title") or ""), author=str(data.get("author_name") or ""))
