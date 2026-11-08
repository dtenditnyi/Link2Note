"""A1: всё от чужого user_id отбрасывается до хендлеров — и сообщения, и кнопки."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from handlers.middlewares import OwnerOnlyMiddleware, setup_owner_filter
from tests.conftest import OWNER, STRANGER


def _user(uid):
    return User(id=uid, is_bot=False, first_name="u")


def _message_update(uid, text="hi"):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=uid, type="private"),
            from_user=_user(uid),
            text=text,
        ),
    )


def _callback_update(uid):
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="1", from_user=_user(uid), chat_instance="x", data="anything"
        ),
    )


@pytest.fixture
def probe():
    calls = []
    router = Router()

    @router.message()
    async def on_msg(message: Message):
        calls.append(("message", message.from_user.id))

    @router.callback_query()
    async def on_cb(cb: CallbackQuery):
        calls.append(("callback", cb.from_user.id))

    dp = Dispatcher()
    setup_owner_filter(dp, OWNER)
    dp.include_router(router)
    return dp, calls


@pytest.fixture
def tg_bot():
    return Bot(token="42:TEST")


@pytest.mark.parametrize("make", [_message_update, _callback_update])
async def test_stranger_dropped(probe, tg_bot, make):
    dp, calls = probe
    await dp.feed_update(tg_bot, make(STRANGER))
    assert calls == []


@pytest.mark.parametrize(
    ("make", "kind"), [(_message_update, "message"), (_callback_update, "callback")]
)
async def test_owner_passes(probe, tg_bot, make, kind):
    dp, calls = probe
    await dp.feed_update(tg_bot, make(OWNER))
    assert calls == [(kind, OWNER)]


async def test_event_without_user_dropped():
    mw = OwnerOnlyMiddleware(OWNER)
    handler = AsyncMock()
    await mw(handler, MagicMock(), {"event_from_user": None})
    handler.assert_not_called()
