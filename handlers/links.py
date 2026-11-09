"""Ссылки в сообщении (текст, подпись, text_link) → очередь Link2Note.

Роутер стоит после reminders и voice, но перед context: «напомни … ссылка» остаётся
напоминанием, а текст без ссылок по-прежнему тихо уходит в контекст.
"""

import asyncio
import re
from typing import Any

from aiogram import Router
from aiogram.types import Message

from digest.classifier import URL_RE, extract_urls, youtube_id
from digest.worker import Job

router = Router(name="links")

MAX_LINKS = 5
MAX_HINT = 300  # длинный текст — скорее чужой пост, чем пожелание
NOT_SUPPORTED = (
    "Пока умею только видео с YouTube (не каналы и плейлисты) — "
    "статьи, Reels и TikTok появятся на этапе 2."
)
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


def _unique_videos(urls: list[str]) -> list[str]:
    """YouTube-ссылки с ID видео; одно видео в разных формах ссылки — один раз."""
    seen: set[str] = set()
    out = []
    for url in urls:
        vid = youtube_id(url)
        if vid and vid not in seen:
            seen.add(vid)
            out.append(url)
    return out


@router.message(has_links)
async def on_links(
    message: Message, urls: list[str], link_queue: asyncio.Queue[Job], vault_ready: bool
) -> None:
    videos = _unique_videos(urls)
    skipped = sum(1 for u in urls if not youtube_id(u))
    if not videos:
        await message.reply(NOT_SUPPORTED)
        return
    if not vault_ready:
        await message.reply(NO_VAULT)
        return

    hint = _hint(message, urls)
    for url in videos[:MAX_LINKS]:
        status = await message.reply(QUEUED)
        link_queue.put_nowait(
            Job(url=url, hint=hint, chat_id=message.chat.id, status_msg_id=status.message_id)
        )
    if len(videos) > MAX_LINKS:
        await message.reply(
            f"Взял первые {MAX_LINKS} ссылок — остальные пришли отдельным сообщением."
        )
    if skipped:
        await message.reply(f"Пока умею только YouTube — пропустил ссылок: {skipped}.")
