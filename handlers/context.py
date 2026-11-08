"""Обычный текст без ссылок → тихо в память как контекст к голосовому.

Роутер подключается последним.

Ссылки сюда не попадают — их заберёт Link2Note (этап 1).
"""

from collections.abc import Callable
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from services.context import ContextMemory, contains_url

router = Router(name="context")


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_text(message: Message, memory: ContextMemory, clock: Callable[[], datetime]) -> None:
    text = message.text or ""
    entities = message.entities or []
    if contains_url(text) or any(e.type in ("url", "text_link") for e in entities):
        return
    memory.add(message.chat.id, text, clock())
