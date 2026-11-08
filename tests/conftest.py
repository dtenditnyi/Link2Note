from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

TZ = ZoneInfo("Europe/Bucharest")
OWNER = 111
STRANGER = 999


@pytest.fixture
def tz() -> ZoneInfo:
    return TZ


@pytest.fixture
def now() -> datetime:
    # Воскресенье, 08.11.2026 12:00 по Бухаресту
    return datetime(2026, 11, 8, 12, 0, tzinfo=TZ)


@pytest.fixture
def db(tmp_path):
    from database import Database

    database = Database(tmp_path / "test.db")
    database.init()
    return database


@pytest.fixture
def bot():
    b = MagicMock()
    b.id = 42
    b.send_message = AsyncMock(return_value=MagicMock(message_id=500))
    b.download = AsyncMock()
    return b


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=42, chat_id=OWNER, user_id=OWNER)
    )


class FakeAPScheduler:
    """Минимальная замена AsyncIOScheduler: только запоминает задачи."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.running = False

    def start(self):
        self.running = True

    def add_job(self, func, trigger, *, id, run_date, replace_existing, kwargs, **opts):
        self.jobs[id] = {"func": func, "run_date": run_date, "kwargs": kwargs, **opts}

    def remove_job(self, job_id):
        from apscheduler.jobstores.base import JobLookupError

        if job_id not in self.jobs:
            raise JobLookupError(job_id)
        del self.jobs[job_id]

    def get_job(self, job_id):
        return self.jobs.get(job_id)


@pytest.fixture
def aps() -> FakeAPScheduler:
    return FakeAPScheduler()


def make_message(text: str | None = "", *, user_id: int = OWNER, chat_id: int = OWNER):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.entities = None
    msg.chat.id = chat_id
    msg.from_user.id = user_id
    msg.from_user.full_name = "Danylo"
    msg.answer = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), message_id=700))
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    return msg


def make_callback(*, user_id: int = OWNER, chat_id: int = OWNER, text: str = ""):
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.message = make_message(text, user_id=user_id, chat_id=chat_id)
    cb.answer = AsyncMock()
    return cb


def sent_texts(mock) -> list[str]:
    """Все тексты, переданные в answer/edit_text/send_message (позиционно или text=)."""
    out = []
    for call in mock.call_args_list:
        if call.args:
            # send_message(chat_id, text) или answer(text)
            out.append(call.args[-1] if len(call.args) > 1 else call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out
