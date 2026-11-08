"""Краткосрочная память текста: контекст к следующему голосовому («сделай это» = этот текст)."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def contains_url(text: str) -> bool:
    return bool(URL_RE.search(text or ""))


@dataclass
class _Entry:
    content: str
    expires_at: datetime


class ContextMemory:
    def __init__(self, ttl: timedelta = timedelta(minutes=15)):
        self.ttl = ttl
        self._items: dict[int, _Entry] = {}

    def add(self, chat_id: int, text: str, now: datetime) -> None:
        entry = self._items.get(chat_id)
        if entry and entry.expires_at > now:
            entry.content += f"\n{text}"
            entry.expires_at = now + self.ttl
        else:
            self._items[chat_id] = _Entry(text, now + self.ttl)

    def get(self, chat_id: int, now: datetime) -> str | None:
        entry = self._items.get(chat_id)
        if entry is None or entry.expires_at <= now:
            return None
        return entry.content

    def pop(self, chat_id: int, now: datetime) -> str | None:
        entry = self._items.pop(chat_id, None)
        if entry is None or entry.expires_at <= now:
            return None
        return entry.content
