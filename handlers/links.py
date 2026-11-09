"""Ссылки в сообщении (текст, подпись, text_link) → очередь Link2Note.

Роутер стоит после reminders и voice, но перед context: «напомни … ссылка» остаётся
напоминанием, а текст без ссылок по-прежнему тихо уходит в контекст.
"""

import asyncio
import re
from typing import Any
from urllib.parse import urlparse

from aiogram import Router
from aiogram.types import Message

from database import Database
from digest.classifier import (
    URL_RE,
    Source,
    classify,
    extract_urls,
    normalize,
    split_force,
    youtube_id,
)
from digest.worker import Job

router = Router(name="links")

MAX_LINKS = 5
MAX_HINT = 300  # длинный текст — скорее чужой пост, чем пожелание
NOT_A_VIDEO = "Это ссылка на YouTube-канал или плейлист — пришли ссылку на конкретный ролик."
NOTHING_IN_FORWARD = "В пересланном посте нет ссылок, которые я умею разбирать."
NO_VAULT = "Хранилище не настроено (VAULT_REPO) — некуда сохранять заметки."
QUEUED = "⏳ Принял ссылку, в очереди…"


def _text(message: Message) -> str:
    return message.text or message.caption or ""


async def has_links(message: Message) -> dict[str, Any] | bool:
    entities = message.entities if message.text else message.caption_entities
    urls = extract_urls(_text(message), entities)
    return {"urls": urls} if urls else False


def _hint(message: Message, urls: list[str]) -> str:
    """Всё, что я написал кроме ссылок, — пожелание к конспекту.

    У пересланного поста текст чужой (часто реклама) — это не моё пожелание.
    """
    if message.forward_origin is not None:
        return ""
    hint = URL_RE.sub(" ", _text(message))
    for url in urls:
        hint = hint.replace(re.sub(r"^https?://", "", url), " ")
    return " ".join(hint.split())[:MAX_HINT]


def _is_youtube_non_video(url: str) -> bool:
    return classify(url) is Source.YOUTUBE and youtube_id(url) is None


def _unique(urls: list[str]) -> list[str]:
    """Одна ссылка в разных формах (youtu.be и watch?v=, ?igsh=…) — один раз."""
    seen: set[str] = set()
    out = []
    for url in urls:
        key = normalize(url)
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out


def _from_forwarded(urls: list[str]) -> list[str]:
    """В чужом посте много лишних ссылок (спонсоры, t.me, магазины): берём видео,
    а если видео нет — только первую ссылку-статью."""
    videos = [u for u in urls if classify(u) is not Source.ARTICLE]
    if videos:
        return videos
    articles = [u for u in urls if not _on_telegram(u)]
    return articles[:1]


def _on_telegram(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"t.me", "telegram.me", "telegram.org"} or host.endswith(".t.me")


@router.message(has_links)
async def on_links(
    message: Message,
    urls: list[str],
    link_queue: asyncio.Queue[Job],
    vault_ready: bool,
    db: Database,
) -> None:
    accepted = _unique([u for u in urls if not _is_youtube_non_video(u)])
    skipped = sum(1 for u in urls if _is_youtube_non_video(u))
    if message.forward_origin is not None and accepted:
        accepted = _from_forwarded(accepted)
        if not accepted:
            await message.reply(NOTHING_IN_FORWARD)
            return
    if not accepted:
        await message.reply(NOT_A_VIDEO)
        return
    if not vault_ready:
        await message.reply(NO_VAULT)
        return

    hint, force = split_force(_hint(message, urls))
    for url in accepted[:MAX_LINKS]:
        status = await message.reply(QUEUED)
        job_id = db.add_job(url, hint, force, message.chat.id, status.message_id)
        link_queue.put_nowait(
            Job(
                url=url,
                hint=hint,
                chat_id=message.chat.id,
                status_msg_id=status.message_id,
                force=force,
                id=job_id,
            )
        )
    if len(accepted) > MAX_LINKS:
        await message.reply(
            f"Взял первые {MAX_LINKS} ссылок — остальные пришли отдельным сообщением."
        )
    if skipped:
        await message.reply(f"Каналы и плейлисты YouTube пропустил: {skipped}.")
