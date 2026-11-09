"""Ссылка → Note(digest, markdown, keys). Общий путь для Telegram-воркера и CLI, без записи.

YouTube — Gemini смотрит по ссылке; Instagram/TikTok/прочее видео — yt-dlp → Files API;
всё остальное — статья (trafilatura → текст). Время каждого шага пишется в лог (⏱).

Gemini не открыл видео (VideoUnreadable, не перегрузка) → запасной путь: звук → faster-whisper →
конспект по расшифровке (`via: whisper`, confidence не выше medium). Стоит +1 запрос Gemini.
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
from digest.errors import DigestError, VideoUnreadable
from digest.fetchers import (
    VideoOptions,
    download_audio,
    download_video,
    fetch_article,
    youtube_meta,
)
from digest.llm import Digested
from digest.render import to_markdown
from digest.schema import Digest

log = logging.getLogger(__name__)

Known = Callable[[list[str]], str | None]
Progress = Callable[[str], Awaitable[None]]
WHISPER_MAX_SECONDS = 30 * 60
WHISPER_START = "🎧 Gemini не открыл видео — расшифровываю звук…"


class DigestModel(Protocol):
    async def digest_youtube(self, url: str, hint: str = "") -> Digested: ...

    async def digest_video_file(self, path: Path, mime_type: str, hint: str = "") -> Digested: ...

    async def digest_text(
        self, text: str, title: str = "", hint: str = "", *, transcript: bool = False
    ) -> Digested: ...


class Transcribes(Protocol):
    async def transcribe_timed(self, path: Path) -> str: ...


class DuplicateError(DigestError):
    """Конспект по этой ссылке уже есть — квоту не тратим."""

    def __init__(self, note_name: str, keys: list[str] | None = None):
        super().__init__(f"Уже есть: «{note_name}»")
        self.note_name = note_name
        self.keys = list(keys or [])


@dataclass(frozen=True)
class Note:
    digest: Digest
    markdown: str
    keys: list[str] = field(default_factory=list)  # ключи для поиска дубликатов
    model: str = ""  # модель Gemini, сделавшая конспект
    url: str = ""  # ссылка, записанная в заметку (у YouTube — каноническая)


@contextmanager
def _timed(step: str, url: str) -> Generator[None]:
    start = time.monotonic()
    try:
        yield
    finally:
        log.info("⏱ %s: %.1f с (%s)", step, time.monotonic() - start, url)


def _check_known(known: Known | None, keys: list[str]) -> None:
    if known is not None and (name := known(keys)):
        raise DuplicateError(name, keys)


def _note(res: Digested, markdown: str, keys: list[str], url: str, note_url: str) -> Note:
    log.info("📝 Конспект: модель %s (%s)", res.model, url)
    return Note(res.digest, markdown, keys, res.model, note_url)


async def _from_transcript(
    path: Path,
    *,
    title: str,
    hint: str,
    llm: DigestModel,
    transcriber: Transcribes,
    progress: Progress,
    url: str,
) -> Digested:
    with _timed("whisper", url):
        text = await transcriber.transcribe_timed(path)
    if not text.strip():
        raise DigestError("В видео нет речи — конспект по звуку не сделать.")
    await progress("🧠 Делаю конспект по расшифровке…")
    with _timed("gemini", url):
        res = await llm.digest_text(text, title=title, hint=hint, transcript=True)
    if res.digest.confidence == "high":
        res.digest.confidence = "medium"  # экрана модель не видела
    return res


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
    transcriber: Transcribes | None = None,
    whisper_max_seconds: int = WHISPER_MAX_SECONDS,
) -> Note:
    """known(keys) → имя заметки, если такая ссылка уже была (тогда DuplicateError).

    transcriber=None — без запасного пути через Whisper (VideoUnreadable уходит наверх).
    """
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
        via = ""
        try:
            with _timed("gemini", url):
                res = await llm.digest_youtube(canonical, hint)
        except VideoUnreadable as err:
            if transcriber is None:
                raise
            log.warning("Gemini не открыл видео (%s) → Whisper (%s)", err, url)
            await progress(WHISPER_START)
            with tempfile.TemporaryDirectory(prefix="l2n_audio_") as tmp:
                try:
                    with _timed("download audio", url):
                        path, audio = await download_audio(
                            canonical,
                            Path(tmp),
                            video or VideoOptions(),
                            max_seconds=whisper_max_seconds,
                        )
                except DigestError as dl_err:
                    raise DigestError(
                        f"Gemini не открыл видео, и звук скачать не вышло: {dl_err}"
                    ) from dl_err
                res = await _from_transcript(
                    path,
                    title=audio.title or meta.title,
                    hint=hint,
                    llm=llm,
                    transcriber=transcriber,
                    progress=progress,
                    url=url,
                )
            via = "whisper"
        md = to_markdown(
            res.digest,
            url=canonical,
            source=src.value,
            author=meta.author,
            date_added=today,
            model=res.model,
            via=via,
        )
        return _note(res, md, keys, url, canonical)

    _check_known(known, keys)

    if src is Source.ARTICLE:
        await progress("📖 Читаю статью…")
        with _timed("article", url):
            art = await fetch_article(url, http)
        await progress("🧠 Делаю конспект…")
        with _timed("gemini", url):
            res = await llm.digest_text(art.text, title=art.title, hint=hint)
        md = to_markdown(
            res.digest,
            url=url,
            source=src.value,
            author=art.author or art.sitename,
            date_added=today,
            model=res.model,
        )
        return _note(res, md, keys, url, url)

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
        via = ""
        try:
            with _timed("gemini", url):
                res = await llm.digest_video_file(path, mime, hint)
        except VideoUnreadable as err:
            if transcriber is None:
                raise
            if meta.duration > whisper_max_seconds:
                raise DigestError(
                    f"Gemini не открыл видео, а расшифровывать звук дольше "
                    f"{whisper_max_seconds // 60} мин не берусь."
                ) from err
            log.warning("Gemini не открыл видео (%s) → Whisper (%s)", err, url)
            await progress(WHISPER_START)
            res = await _from_transcript(  # файл уже скачан — второй раз не качаем
                path,
                title=meta.title,
                hint=hint,
                llm=llm,
                transcriber=transcriber,
                progress=progress,
                url=url,
            )
            via = "whisper"
    md = to_markdown(
        res.digest,
        url=url,
        source=src.value,
        author=meta.author,
        date_added=today,
        duration=meta.duration,
        model=res.model,
        via=via,
    )
    return _note(res, md, keys, url, url)
