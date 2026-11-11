"""Черновики голосовых напоминаний до «✅ Сохранить».

Ключ — (chat_id, message_id сообщения с подтверждением): у каждого голосового свой черновик,
и он не зависит от FSM (не стирается /start, /cancel или другим сценарием). Живёт ttl.
"""

from datetime import datetime, timedelta

from services.intent import ParsedReminder

DraftKey = tuple[int, int]


class DraftStore:
    def __init__(self, ttl: timedelta = timedelta(minutes=30)):
        self.ttl = ttl
        self._items: dict[DraftKey, tuple[ParsedReminder, datetime]] = {}

    def put(self, key: DraftKey, draft: ParsedReminder, now: datetime) -> None:
        self._items = {k: v for k, v in self._items.items() if v[1] > now}  # чистим протухшие
        self._items[key] = (draft, now + self.ttl)

    def pop(self, key: DraftKey, now: datetime) -> ParsedReminder | None:
        item = self._items.pop(key, None)
        if item is None or item[1] <= now:
            return None
        return item[0]


class TextStore:
    """Расшифровки голосовых для «🔁 Повторить», когда Gemini был недоступен (живут ttl)."""

    def __init__(self, ttl: timedelta = timedelta(hours=6)):
        self.ttl = ttl
        self._items: dict[DraftKey, tuple[str, datetime]] = {}

    def put(self, key: DraftKey, text: str, now: datetime) -> None:
        self._items = {k: v for k, v in self._items.items() if v[1] > now}
        self._items[key] = (text, now + self.ttl)

    def pop(self, key: DraftKey, now: datetime) -> str | None:
        item = self._items.pop(key, None)
        if item is None or item[1] <= now:
            return None
        return item[0]
