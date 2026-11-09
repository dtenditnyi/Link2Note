"""Получение материала без LLM: метаданные YouTube, скачивание видео (yt-dlp), текст статьи.

YouTube oEmbed — заодно ранняя проверка доступности: приватное или удалённое видео отсекаем
до Gemini, не тратя квоту. Любые другие сбои oEmbed не мешают конспекту — просто без автора.
"""

import asyncio
import logging
import re
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura
import yt_dlp
from curl_cffi import CurlError
from yt_dlp.utils import DownloadError

from digest.errors import DigestError

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"

# Сначала готовый файл со звуком (слияние дорожек требует ffmpeg), потом — слияние.
VIDEO_FORMAT = "b[height<=720]/bv*[height<=720]+ba/b"
ARTICLE_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_MIN_CHARS = 300
ARTICLE_TIMEOUT_S = 30
_HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


@dataclass(frozen=True)
class VideoMeta:
    title: str = ""
    author: str = ""
    duration: int = 0  # секунды; 0 — неизвестно
    key: str = ""  # «<extractor>:<id>» от yt-dlp — второй ключ для дубликатов


@dataclass(frozen=True)
class VideoOptions:
    cookies_file: Path | None = None
    max_seconds: int = 3600
    max_mb: int = 300


@dataclass(frozen=True)
class Article:
    title: str
    author: str
    sitename: str
    text: str


# --- YouTube oEmbed ------------------------------------------------------------------------


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


# --- yt-dlp ------------------------------------------------------------------------------


def cookies_warning(path: Path | None) -> str | None:
    """Текст предупреждения, если COOKIES_FILE указан, а файла нет (например, путь из Docker)."""
    if path is None or path.is_file():
        return None
    return f"COOKIES_FILE={path}: файла нет — видео качаю без входа (Instagram может не отдать)."


def _download_error(err: DownloadError, cookies: Path | None, has_cookies: bool) -> DigestError:
    text = str(err)
    low = text.lower()
    if "login required" in low or "rate-limit" in low or "cookies" in low:
        if cookies is not None and not has_cookies:
            return DigestError(
                f"Сайт не отдаёт видео без входа, а COOKIES_FILE указан ({cookies}), "
                "но файла нет — положи туда cookies.txt этого сайта."
            )
        if has_cookies:
            return DigestError(
                "Сайт требует вход, а cookies из COOKIES_FILE не подошли — похоже, устарели. "
                "Экспортируй cookies.txt заново."
            )
        return DigestError(
            "Сайт не отдаёт видео без входа — добавь cookies.txt этого сайта "
            "и путь к нему в COOKIES_FILE."
        )
    if "no video" in low or "no video formats" in low or "there's no video" in low:
        return DigestError("В этом посте нет видео.")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    last = (lines[-1] if lines else "ошибка yt-dlp").removeprefix("ERROR:").strip()
    return DigestError(f"Не смог скачать видео: {last[:200]}")


def _download_sync(
    url: str, out_dir: Path, opts: VideoOptions, ydl_factory: Callable[..., Any]
) -> tuple[Path, VideoMeta]:
    params: dict[str, Any] = {
        "format": VIDEO_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": opts.max_mb * 1024 * 1024,
        "cachedir": False,
    }
    has_cookies = False
    if warning := cookies_warning(opts.cookies_file):
        log.warning(warning)  # не ошибка: многим сайтам вход не нужен
    elif opts.cookies_file is not None:
        # yt-dlp при закрытии перезаписывает cookiefile — отдаём ему копию.
        copy = out_dir / "cookies.txt"
        shutil.copyfile(opts.cookies_file, copy)
        params["cookiefile"] = str(copy)
        has_cookies = True

    try:
        with ydl_factory(params) as ydl:
            # Одно извлечение на ссылку: каждый лишний запрос к Instagram/TikTok — риск бана.
            info = ydl.extract_info(url, download=False)
            if info.get("_type") in ("playlist", "multi_video"):  # карусель Instagram и т.п.
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    raise DigestError("В этом посте нет видео.")
                info = entries[0]
            duration = int(round(info.get("duration") or 0))
            if duration > opts.max_seconds:
                raise DigestError(f"Видео длиннее {opts.max_seconds // 60} мин — такое не беру.")
            info = ydl.process_ie_result(info, download=True)
    except DownloadError as err:
        raise _download_error(err, opts.cookies_file, has_cookies) from err

    # Точный путь после слияния дорожек; поиск по папке мог бы взять недокачанный .part.
    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    path = Path(filepath) if filepath else None
    if path is None or not path.is_file():
        # max_filesize молча пропускает скачивание — файла нет.
        raise DigestError(f"Видео больше {opts.max_mb} МБ или не скачалось.")

    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    vid = str(info.get("id") or "")
    meta = VideoMeta(
        title=str(info.get("title") or ""),
        author=str(info.get("uploader") or info.get("channel") or ""),
        duration=duration,
        key=f"{extractor}:{vid}" if extractor and vid else "",
    )
    return path, meta


async def download_video(
    url: str,
    out_dir: Path,
    opts: VideoOptions,
    *,
    ydl_factory: Callable[..., Any] = yt_dlp.YoutubeDL,
) -> tuple[Path, VideoMeta]:
    """Скачивает видео в out_dir (папку удаляет вызывающий). yt-dlp синхронный — в потоке."""
    return await asyncio.to_thread(_download_sync, url, Path(out_dir), opts, ydl_factory)


# --- Статьи ------------------------------------------------------------------------------

# Подвал, навигация и служебные блоки, которые trafilatura иногда принимает за автора.
# Английские слова — целиком (Menuhin, Shareef — имена), русские основы — с начала слова.
_JUNK_AUTHOR_RE = re.compile(
    r"\b(?:databases?|navigation|menus?|cookies?|privacy|copyright|subscribe|newsletters?"
    r"|share|comments?|related|sign in|log in|authority control|rights reserved)\b"
    r"|(?<!\w)(?:меню|навигац|подпис|рассылк|комментар|поделиться|войти|все права"
    r"|читайте также)"
    r"|©",
    re.IGNORECASE,
)
MAX_AUTHOR_CHARS = 60
MAX_AUTHOR_WORDS = 6


@dataclass(frozen=True)
class RawPage:
    status: int
    content_type: str
    body: bytes  # пусто, если статус ≥ 400 или это не HTML


def clean_author(author: str, sitename: str = "") -> str:
    """Автор, если это похоже на имя; мусор из подвала/навигации → ""."""
    name = " ".join((author or "").split())
    low = name.lower()
    if (
        not name
        or len(name) > MAX_AUTHOR_CHARS
        or len(name.split()) > MAX_AUTHOR_WORDS
        or _JUNK_AUTHOR_RE.search(name)
        or "http" in low
        or "www." in low
        or "@" in name
        or (sitename and low == sitename.strip().lower())
    ):
        return ""
    return name


def _is_html(content_type: str) -> bool:
    ctype = content_type.split(";")[0].strip().lower()
    return not ctype or ctype in _HTML_TYPES


async def _read_limited(chunks: AsyncIterator[bytes], declared: str | None) -> bytes:
    if declared and declared.isdigit() and int(declared) > ARTICLE_MAX_BYTES:
        raise DigestError("Страница слишком большая (больше 5 МБ).")
    out: list[bytes] = []
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > ARTICLE_MAX_BYTES:
            raise DigestError("Страница слишком большая (больше 5 МБ).")
        out.append(chunk)
    return b"".join(out)


async def _httpx_get(url: str, http: httpx.AsyncClient) -> RawPage:
    async with http.stream(
        "GET",
        url,
        headers={"User-Agent": _BROWSER_UA, "Accept-Language": "ru,en;q=0.8"},
        follow_redirects=True,
        timeout=20,
    ) as resp:
        ctype = resp.headers.get("content-type", "")
        if resp.status_code >= 400 or not _is_html(ctype):
            return RawPage(resp.status_code, ctype, b"")
        body = await _read_limited(resp.aiter_bytes(), resp.headers.get("content-length"))
        return RawPage(resp.status_code, ctype, body)


async def _impersonated_get(url: str) -> RawPage:
    """Повтор под видом настоящего браузера (TLS-отпечаток Chrome) — против антиботов."""
    from curl_cffi.requests import AsyncSession

    async with (
        AsyncSession(impersonate="chrome") as session,
        session.stream("GET", url, timeout=20, allow_redirects=True) as resp,
    ):
        try:
            ctype = resp.headers.get("content-type") or ""
            if resp.status_code >= 400 or not _is_html(ctype):
                return RawPage(resp.status_code, ctype, b"")
            body = await _read_limited(resp.aiter_content(), resp.headers.get("content-length"))
            return RawPage(resp.status_code, ctype, body)
        finally:
            # Async aclose() curl_cffi ждёт конца передачи и сам её не прерывает:
            # без этого большая страница/PDF докачивалась бы в память до таймаута.
            if resp.quit_now is not None:
                resp.quit_now.set()


def _check(page: RawPage) -> None:
    code = page.status
    if code in (401, 402):
        raise DigestError(f"Страница закрыта (нужен вход или paywall, код {code}).")
    if code in (403, 429):
        raise DigestError(
            f"Сайт блокирует автоматическое чтение (антибот, код {code}) — "
            "открой статью сам или пришли её текст."
        )
    if code == 404:
        raise DigestError("Страница не найдена (404).")
    if code >= 400:
        raise DigestError(f"Сайт ответил ошибкой {code}.")
    if not _is_html(page.content_type):
        ctype = page.content_type.split(";")[0].strip().lower()
        raise DigestError(f"Пока умею только веб-страницы, а это {ctype} (PDF и файлы — не умею).")


def _site_from_url(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _extract(html: bytes, url: str) -> Article:
    doc = trafilatura.bare_extraction(
        html, url=url, with_metadata=True, include_comments=False, include_tables=True
    )
    text = (getattr(doc, "text", None) or "").strip() if doc is not None else ""
    if len(text) < ARTICLE_MIN_CHARS:
        raise DigestError(
            "Не смог вытащить текст статьи — сайт, похоже, грузит текст через JavaScript "
            "или закрыт paywall."
        )
    sitename = str(getattr(doc, "sitename", None) or "").strip() or _site_from_url(url)
    return Article(
        title=str(getattr(doc, "title", None) or ""),
        author=clean_author(str(getattr(doc, "author", None) or ""), sitename),
        sitename=sitename,
        text=text,
    )


async def fetch_article(url: str, http: httpx.AsyncClient) -> Article:
    # timeout у httpx — на каждое чтение; медленный сайт «по капле» держал бы очередь.
    try:
        async with asyncio.timeout(ARTICLE_TIMEOUT_S):
            page = await _httpx_get(url, http)
        if page.status in (403, 429):
            log.info("%s ответил %s — повтор через curl-cffi (impersonate)", url, page.status)
            async with asyncio.timeout(ARTICLE_TIMEOUT_S):
                page = await _impersonated_get(url)
    except TimeoutError as err:
        raise DigestError("Сайт слишком долго отдаёт страницу.") from err
    except (httpx.HTTPError, CurlError) as err:
        raise DigestError(f"Не смог открыть страницу: {type(err).__name__}") from err
    _check(page)
    return await asyncio.to_thread(_extract, page.body, url)
