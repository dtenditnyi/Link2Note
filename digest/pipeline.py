"""Ссылка → Note(digest, markdown, keys). Общий путь для Telegram-воркера и CLI, без записи.

YouTube — Gemini смотрит по ссылке; Instagram/TikTok/прочее видео — yt-dlp → Files API;
всё остальное — статья (trafilatura → текст). Время каждого шага пишется в лог (⏱).
"""

import logging
import mimetypes
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

import httpx

from digest.classifier import Source, canonical_youtube, classify, normalize, youtube_id
from digest.errors import DigestError
from digest.fetchers import VideoOptions, download_video, fetch_article, youtube_meta
from digest.render import to_markdown
from digest.schema import Digest

log = logging.getLogger(__name__)

Known = Callable[[list[str]], str | None]
Progress = Callable[[str], Awaitable[None]]


class DigestModel(Protocol):
    async def digest_youtube(self, url: str, hint: str = "") -> Digest: ...

    async def digest_video_file(self, path: Path, mime_type: str, hint: str = "") -> Digest: ...

    async def digest_text(self, text: str, title: str = "", hint: str = "") -> Digest: ...


class DuplicateError(DigestError):
    """Конспект по этой ссылке уже есть — квоту не тратим."""

    def __init__(self, note_name: str):
        super().__init__(f"Уже есть: «{note_name}»")
        self.note_name = note_name


@dataclass(frozen=True)
class Note:
    digest: Digest
    markdown: str
    keys: list[str] = field(default_factory=list)  # ключи для поиска дубликатов


@contextmanager
def _timed(step: str, url: str) -> Generator[None]:
    start = time.monotonic()
    try:
        yield
    finally:
        log.info("⏱ %s: %.1f с (%s)", step, time.monotonic() - start, url)


def _check_known(known: Known | None, keys: list[str]) -> None:
    if known is not None and (name := known(keys)):
        raise DuplicateError(name)


async def _noop(_: str) -> None:
    return None


async def make_note(
    url: str,
    hint: str,
    *,
    llm: DigestModel,
    http: httpx.AsyncClient,
    today: date,
    video: VideoOptions | None = None,
    known: Known | None = None,
    progress: Progress | None = None,
) -> Note:
    """known(keys) → имя заметки, если такая ссылка уже была (тогда DuplicateError)."""
    progress = progress or _noop
    src = classify(url)
    keys = [normalize(url)]

    if src is Source.YOUTUBE:
        vid = youtube_id(url)
        if vid is None:
            raise DigestError(
                "Это не ссылка на видео (канал или плейлист?) — пришли ссылку на ролик."
            )
        _check_known(known, keys)
        canonical = canonical_youtube(vid)
        await progress("🧠 Смотрю видео…")
        with _timed("oembed", url):
            meta = await youtube_meta(canonical, http)  # приватное/удалённое → до Gemini
        with _timed("gemini", url):
            d = await llm.digest_youtube(canonical, hint)
        md = to_markdown(d, url=canonical, source=src.value, author=meta.author, date_added=today)
        return Note(d, md, keys)

    _check_known(known, keys)

    if src is Source.ARTICLE:
        await progress("📖 Читаю статью…")
        with _timed("article", url):
            art = await fetch_article(url, http)
        await progress("🧠 Делаю конспект…")
        with _timed("gemini", url):
            d = await llm.digest_text(art.text, title=art.title, hint=hint)
        md = to_markdown(
            d, url=url, source=src.value, author=art.author or art.sitename, date_added=today
        )
        return Note(d, md, keys)

    # Instagram, TikTok и прочие сайты с видео — скачиваем и отдаём файлом.
    with tempfile.TemporaryDirectory(prefix="l2n_") as tmp:
        await progress("⬇️ Скачиваю видео…")
        with _timed("download", url):
            path, meta = await download_video(url, Path(tmp), video or VideoOptions())
        if meta.key and meta.key not in keys:
            keys.append(meta.key)
            _check_known(known, keys)  # короткая ссылка vm.tiktok.com → тот же ролик
        await progress("🧠 Смотрю видео…")
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        with _timed("gemini", url):
            d = await llm.digest_video_file(path, mime, hint)
    md = to_markdown(
        d,
        url=url,
        source=src.value,
        author=meta.author,
        date_added=today,
        duration=meta.duration,
    )
    return Note(d, md, keys)
