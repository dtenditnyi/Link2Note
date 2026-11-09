"""Ссылки из сообщения: поиск, тип источника, нормализация (YouTube → ID видео)."""

import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qs, urlparse

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_TRAILING = ".,;:!?»\"'…"
_BRACKETS = {")": "(", "]": "[", "}": "{"}


class Source(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    VIDEO = "video"  # другие сайты, которые понимает yt-dlp (этап 2)
    ARTICLE = "article"


YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "youtube-nocookie.com"}
VIDEO_HOSTS = {"x.com", "twitter.com", "vimeo.com", "reddit.com", "facebook.com"}
_YT_PATH_PREFIXES = ("shorts", "live", "embed", "v")


class _Entity(Protocol):
    type: str
    offset: int
    length: int
    url: str | None


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    return host


def _on_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify(url: str) -> Source:
    host = _host(url)
    if host in YOUTUBE_HOSTS:
        return Source.YOUTUBE
    if _on_domain(host, "instagram.com"):
        return Source.INSTAGRAM
    if _on_domain(host, "tiktok.com"):
        return Source.TIKTOK
    if host in VIDEO_HOSTS:
        return Source.VIDEO
    return Source.ARTICLE


def youtube_id(url: str) -> str | None:
    """ID видео из любой формы YouTube-ссылки; None — канал, плейлист или не YouTube."""
    if classify(url) is not Source.YOUTUBE:
        return None
    u = urlparse(url)
    segments = [s for s in u.path.split("/") if s]
    candidate = ""
    if _host(url) == "youtu.be":
        candidate = segments[0] if segments else ""
    elif segments[:1] == ["watch"]:
        candidate = parse_qs(u.query).get("v", [""])[0]
    elif len(segments) >= 2 and segments[0] in _YT_PATH_PREFIXES:
        candidate = segments[1]
    return candidate if _VIDEO_ID_RE.fullmatch(candidate) else None


def canonical_youtube(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize(url: str) -> str:
    """Ключ для поиска дубликатов: youtube:ID или host/path без query и «/» в конце."""
    if vid := youtube_id(url):
        return f"youtube:{vid}"
    return f"{_host(url)}{urlparse(url).path}".rstrip("/")


def _trim(url: str) -> str:
    """Убираем пунктуацию, прилипшую к ссылке в тексте: «…/ID).» → «…/ID»."""
    while url:
        last = url[-1]
        if last in _TRAILING:
            url = url[:-1]
        elif last in _BRACKETS and url.count(_BRACKETS[last]) < url.count(last):
            url = url[:-1]
        else:
            break
    return url


def _utf16_to_index(text: str, offset: int) -> int:
    """Telegram считает смещения entities в UTF-16; переводим в индекс строки Python."""
    units = 0
    for i, ch in enumerate(text):
        if units >= offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def extract_urls(text: str | None, entities: Sequence[_Entity] | None) -> list[str]:
    """Все ссылки сообщения по порядку, без повторов: из текста и из url/text_link-entities."""
    text = text or ""
    found: list[tuple[int, str]] = []
    for e in entities or []:
        start = _utf16_to_index(text, e.offset)
        if e.type == "text_link" and e.url:
            found.append((start, e.url))
        elif e.type == "url":
            end = _utf16_to_index(text, e.offset + e.length)
            value = _trim(text[start:end])
            if not re.match(r"https?://", value, re.IGNORECASE):
                value = f"https://{value}"
            found.append((start, value))
    for m in URL_RE.finditer(text):
        found.append((m.start(), _trim(m.group())))

    seen: set[str] = set()
    out: list[str] = []
    for _, url in sorted(found, key=lambda item: item[0]):
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out
