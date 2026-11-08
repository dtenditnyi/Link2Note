from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from database import Database
from handlers.keyboards import RepeatCb
from handlers.reminders import CreateReminder, EditReminder
from main import build_dispatcher
from services.context import ContextMemory
from tests.conftest import OWNER, STRANGER, TZ
from tests.test_middlewares import _callback_update, _message_update


@pytest.fixture(scope="module")
def dp(tmp_path_factory):
    # Роутеры модульные и подключаются к диспетчеру один раз — собираем его один раз на модуль.
    db = Database(tmp_path_factory.mktemp("main") / "t.db")
    db.init()
    return build_dispatcher(
        owner_id=OWNER,
        db=db,
        reminders=MagicMock(),
        transcriber=MagicMock(),
        intent=MagicMock(),
        memory=ContextMemory(ttl=timedelta(minutes=15)),
        tz=TZ,
        voice_max_seconds=120,
    )


def test_routers_order_context_last(dp):
    names = [r.name for r in dp.sub_routers]
    assert names == ["basic", "reminders", "voice", "context"]


def test_workflow_data_injected(dp):
    for key in (
        "db",
        "reminders",
        "transcriber",
        "intent",
        "memory",
        "drafts",
        "tz",
        "clock",
        "voice_max_seconds",
    ):
        assert key in dp.workflow_data
    assert dp.workflow_data["clock"]().tzinfo is not None


async def test_stranger_gets_nothing_through_real_dispatcher(dp):
    """A1 на собранном боте: /start и кнопка от чужого не вызывают Telegram API."""
    bot = Bot(token="42:TEST")
    # Любой вызов API упал бы с сетевой ошибкой — значит, ничего не вызвано.
    await dp.feed_update(bot, _message_update(STRANGER, "/start"))
    await dp.feed_update(bot, _callback_update(STRANGER))
    await bot.session.close()


# --- Маршрутизация через настоящий диспетчер (фейковая HTTP-сессия) -------------------


class FakeSession(BaseSession):
    """Ничего не шлёт в Telegram, только запоминает вызванные методы API."""

    def __init__(self):
        super().__init__()
        self.calls = []

    # Сигнатура задана BaseSession aiogram — имя timeout менять нельзя.
    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def close(self):
        pass


@pytest.fixture
def fake_bot():
    return Bot(token="42:TEST", session=FakeSession())


def _key():
    return StorageKey(bot_id=42, chat_id=OWNER, user_id=OWNER)


def _memory(dp):
    return dp.workflow_data["memory"].get(OWNER, dp.workflow_data["clock"]())


async def _reset(dp):
    await dp.fsm.storage.set_state(_key(), None)
    await dp.fsm.storage.set_data(_key(), {})
    dp.workflow_data["memory"].pop(OWNER, dp.workflow_data["clock"]())


async def test_text_during_fsm_goes_to_fsm_not_context(dp, fake_bot):
    """Текст в состоянии CreateReminder.task обрабатывает FSM, а не контекст-роутер."""
    await _reset(dp)
    await dp.fsm.storage.set_state(_key(), CreateReminder.task)
    await dp.feed_update(fake_bot, _message_update(OWNER, "Выпить воды"))
    assert await dp.fsm.storage.get_state(_key()) == CreateReminder.time.state
    assert _memory(dp) is None


async def test_plain_text_goes_to_context_silently(dp, fake_bot):
    """A4 на собранном боте: текст без ссылок — в память, ни одного вызова API."""
    await _reset(dp)
    await dp.feed_update(fake_bot, _message_update(OWNER, "рецепт пирога"))
    assert _memory(dp) == "рецепт пирога"
    assert fake_bot.session.calls == []


async def test_link_not_stored_through_dispatcher(dp, fake_bot):
    """A3 на собранном боте: ссылка не попадает в контекст и не получает ответа."""
    await _reset(dp)
    await dp.feed_update(fake_bot, _message_update(OWNER, "https://youtu.be/abc"))
    assert _memory(dp) is None
    assert fake_bot.session.calls == []


async def test_stale_repeat_button_through_dispatcher(dp, fake_bot):
    """Кнопка повтора вне сценария создания: только ответ на callback, состояние не тронуто."""
    await _reset(dp)
    await dp.fsm.storage.set_state(_key(), EditReminder.time)
    update = Update(
        update_id=3,
        callback_query=CallbackQuery(
            id="9",
            from_user=User(id=OWNER, is_bot=False, first_name="u"),
            chat_instance="x",
            data=RepeatCb(value="none").pack(),
            message=Message(
                message_id=5, date=datetime.now(), chat=Chat(id=OWNER, type="private"), text="?"
            ),
        ),
    )
    await dp.feed_update(fake_bot, update)
    assert await dp.fsm.storage.get_state(_key()) == EditReminder.time.state
    assert [type(m).__name__ for m in fake_bot.session.calls] == ["AnswerCallbackQuery"]
