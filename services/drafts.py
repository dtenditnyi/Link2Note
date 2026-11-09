"""Временные хранилища голосовых: черновики до «✅ Сохранить» и тексты для «🔁 Повторить».

Ключ — (chat_id, message_id сообщения бота): у каждого голосового своя запись,
не зависящая от FSM (не стирается /start, /cancel или другим сценарием). Живёт ttl.
"""

from datetime import datetime, timedelta

from services.intent import ParsedReminder

Key = tuple[int, int]
DraftKey = Key  # совместимость


class TtlStore[T]:
    def __init__(self, ttl: timedelta):
        self.ttl = ttl
        self._items: dict[Key, tuple[T, datetime]] = {}

    def put(self, key: Key, value: T, now: datetime) -> None:
        self._items = {k: v for k, v in self._items.items() if v[1] > now}  # чистим протухшие
        self._items[key] = (value, now + self.ttl)

    def get(self, key: Key, now: datetime) -> T | None:
        item = self._items.get(key)
        if item is None or item[1] <= now:
            return None
        return item[0]

    def pop(self, key: Key, now: datetime) -> T | None:
        value = self.get(key, now)
        self._items.pop(key, None)
        return value


class DraftStore(TtlStore[ParsedReminder]):
    def __init__(self, ttl: timedelta = timedelta(minutes=30)):
        super().__init__(ttl)


class TranscriptStore(TtlStore[str]):
    """Распознанный текст, если Gemini недоступен, — чтобы «Повторить» без новой записи."""

    def __init__(self, ttl: timedelta = timedelta(hours=6)):
        super().__init__(ttl)
