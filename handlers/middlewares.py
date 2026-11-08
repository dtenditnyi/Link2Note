"""Бот отвечает только владельцу. Фильтр на уровне всех апдейтов — до любых хендлеров."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Dispatcher
from aiogram.types import TelegramObject, User

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            log.info("Ignored update from %s", user.id if user else "unknown")
            return None
        return await handler(event, data)


def setup_owner_filter(dp: Dispatcher, owner_id: int) -> None:
    # Outer-middleware на update: срабатывает после UserContextMiddleware aiogram
    # (event_from_user уже есть) и до фильтров/хендлеров — и для сообщений, и для кнопок.
    dp.update.outer_middleware(OwnerOnlyMiddleware(owner_id))
