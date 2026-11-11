"""Интерфейс хранилища заметок: git-клон (`GitVault`) или API сайта Second Brain (`ApiVault`).

Хендлеры, воркер, воскресный дайджест и CLI зависят только от `VaultBackend`. Ошибки — общие
(`digest.errors`): `NoteNotFound`, `NoteAmbiguous`, `VaultError` и для API — `VaultUnavailable`
(повторить позже), `VaultRejected` (не повторять), `VaultQueued` (лежит в очереди, уйдёт сама).
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class UnreadNote:
    name: str  # имя файла без .md
    title: str  # первый заголовок «# …» (с эмодзи)
    date_added: date | None
    read_minutes: int


@runtime_checkable
class VaultBackend(Protocol):
    async def ready(self) -> bool:
        """Можно ли сейчас писать (git: есть клон; API: GET /health отвечает)."""
        ...

    async def save(self, title: str, markdown: str, day: date) -> str:
        """Новая заметка в папке бота; возвращает её имя (без .md)."""
        ...

    async def set_status(
        self, name: str, url: str, status: str, *, skip: Collection[str] = ()
    ) -> str:
        """status во frontmatter; возвращает текущее имя заметки (её могли переименовать)."""
        ...

    async def replace(
        self, name: str, url: str, markdown: str, *, skip: Collection[str] = ()
    ) -> str:
        """Новый текст той же заметки («Подробнее»); возвращает текущее имя."""
        ...

    async def delete(self, name: str, url: str, *, skip: Collection[str] = ()) -> None: ...

    async def unread(self) -> list[UnreadNote]:
        """Непрочитанные конспекты папки бота, самые старые первыми."""
        ...

    async def find_by_url(self, url: str) -> list[str] | None:
        """Имена заметок с этой ссылкой; None — хранилище не знает (проверять по своей базе)."""
        ...
