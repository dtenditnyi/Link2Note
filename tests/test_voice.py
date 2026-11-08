import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from handlers import voice as v
from scheduler import ReminderScheduler
from services.context import ContextMemory
from services.drafts import DraftStore
from services.intent import IntentError, ParsedReminder
from services.timeparse import Repeat
from services.transcribe import Transcriber
from tests.conftest import OWNER, TZ, make_callback, make_message, sent_texts

ROOT = Path(__file__).resolve().parents[1]

# --- Transcriber (V1, V2) -----------------------------------------------------------


class FakeWhisper:
    def __init__(self):
        self.threads = []

    def transcribe(self, path, **kwargs):
        self.threads.append(threading.get_ident())
        self.kwargs = kwargs
        return iter([SimpleNamespace(text=" Напомни"), SimpleNamespace(text=" выпить воды ")]), None


async def test_transcriber_lazy_load_once_and_off_loop(tmp_path):
    model = FakeWhisper()
    factory = MagicMock(return_value=model)
    t = Transcriber("small", "cpu", "int8", model_factory=factory)
    factory.assert_not_called()  # V1

    text = await t.transcribe(tmp_path / "a.ogg")
    await t.transcribe(tmp_path / "b.ogg")
    assert text == "Напомни выпить воды"
    factory.assert_called_once_with("small", device="cpu", compute_type="int8")
    assert model.threads[0] != threading.get_ident()  # V2: не в event loop
    assert model.kwargs["language"] == "ru"


def test_importing_bot_modules_does_not_load_whisper():
    """V1: импорт хендлеров и main не тянет faster_whisper."""
    code = (
        "import sys, handlers.voice, main; "
        "assert 'faster_whisper' not in sys.modules, 'whisper imported'"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- Голосовое → черновик (V3–V6, V8) ----------------------------------------------


def voice_message(duration=10, status_id=700):
    msg = make_message(None)
    msg.voice = SimpleNamespace(duration=duration, file_unique_id="uniq")
    status = MagicMock(edit_text=AsyncMock(), message_id=status_id)
    msg.answer = AsyncMock(return_value=status)
    return msg, status


@pytest.fixture
def deps(bot, db, aps, now):
    downloaded = []

    async def download(file, destination):
        downloaded.append(Path(destination))

    bot.download = AsyncMock(side_effect=download)
    transcriber = MagicMock()
    transcriber.transcribe = AsyncMock(return_value="напомни через 2 минуты выпить воды")
    intent = MagicMock()
    intent.parse = AsyncMock(
        return_value=ParsedReminder("Выпить <воды>", now + timedelta(minutes=2), Repeat.NONE)
    )
    return SimpleNamespace(
        bot=bot,
        db=db,
        transcriber=transcriber,
        intent=intent,
        memory=ContextMemory(ttl=timedelta(minutes=15)),
        drafts=DraftStore(ttl=timedelta(minutes=30)),
        downloaded=downloaded,
        reminders=ReminderScheduler(bot, db, TZ, scheduler=aps, clock=lambda: now),
        clock=lambda: now,
    )


async def call_voice(msg, d, max_seconds=120):
    await v.on_voice(
        msg,
        bot=d.bot,
        drafts=d.drafts,
        transcriber=d.transcriber,
        intent=d.intent,
        memory=d.memory,
        clock=d.clock,
        tz=TZ,
        voice_max_seconds=max_seconds,
    )


async def test_too_long_voice_refused(deps):
    """V4."""
    msg, _ = voice_message(duration=121)
    await call_voice(msg, deps)
    deps.bot.download.assert_not_called()
    deps.transcriber.transcribe.assert_not_called()
    assert "120" in sent_texts(msg.answer)[0]


async def test_voice_creates_draft_not_reminder(deps, now):
    """V8 + A5: черновик в хранилище, в БД пусто, текст экранирован, кнопки знают сообщение."""
    msg, status = voice_message()
    await call_voice(msg, deps)
    assert deps.db.list_pending(OWNER) == []
    text = sent_texts(status.edit_text)[-1]
    assert "Понял так" in text and "Выпить &lt;воды&gt;" in text
    markup = status.edit_text.call_args.kwargs["reply_markup"]
    save = v.VoiceCb.unpack(markup.inline_keyboard[0][0].callback_data)
    assert (save.action, save.mid) == ("save", 700)
    assert deps.drafts.pop((OWNER, 700), now).task == "Выпить <воды>"


async def test_temp_file_removed_on_success(deps):
    """V3."""
    msg, _ = voice_message()
    await call_voice(msg, deps)
    [path] = deps.downloaded
    assert not path.exists()
    assert not path.parent.exists()
    assert path.parent.resolve() != ROOT


async def test_temp_file_removed_on_error(deps):
    """V3."""
    deps.transcriber.transcribe.side_effect = RuntimeError("whisper crashed")
    msg, status = voice_message()
    await call_voice(msg, deps)
    [path] = deps.downloaded
    assert not path.parent.exists()
    assert "Не удалось" in sent_texts(status.edit_text)[-1]


async def test_context_passed_and_cleared(deps, now):
    """V6."""
    deps.memory.add(OWNER, "https://eda.ru/pirog", now)
    msg, _ = voice_message()
    await call_voice(msg, deps)
    args = deps.intent.parse.call_args.args
    assert args[1] == "https://eda.ru/pirog"
    assert deps.memory.pop(OWNER, now) is None


async def test_context_kept_when_llm_fails(deps, now):
    """V6: ошибка Gemini не съедает контекст — можно повторить голосовое."""
    deps.memory.add(OWNER, "https://eda.ru/pirog", now)
    deps.intent.parse.side_effect = RuntimeError("503")
    msg, _ = voice_message()
    await call_voice(msg, deps)
    assert deps.memory.pop(OWNER, now) == "https://eda.ru/pirog"


async def test_intent_error_reported(deps, now):
    """V5."""
    deps.intent.parse.side_effect = IntentError("время в прошлом")
    msg, status = voice_message()
    await call_voice(msg, deps)
    assert deps.drafts.pop((OWNER, 700), now) is None
    assert "время в прошлом" in sent_texts(status.edit_text)[-1]


async def test_empty_transcript(deps):
    deps.transcriber.transcribe.return_value = "  "
    msg, status = voice_message()
    await call_voice(msg, deps)
    deps.intent.parse.assert_not_called()
    assert "расслышал" in sent_texts(status.edit_text)[-1]


# --- Подтверждение (V8) -----------------------------------------------------------


async def make_draft(deps, status_id=700, task="Выпить <воды>"):
    deps.intent.parse.return_value = ParsedReminder(
        task, deps.clock() + timedelta(minutes=2), Repeat.NONE
    )
    msg, _ = voice_message(status_id=status_id)
    await call_voice(msg, deps)


async def confirm(deps, action, mid=700):
    cb = make_callback()
    await v.on_voice_confirm(
        cb,
        v.VoiceCb(action=action, mid=mid),
        drafts=deps.drafts,
        db=deps.db,
        reminders=deps.reminders,
        tz=TZ,
        clock=deps.clock,
    )
    return cb


async def test_save_creates_reminder(deps, aps, now):
    await make_draft(deps)
    cb = await confirm(deps, "save")
    [r] = deps.db.list_pending(OWNER)
    assert (r.task, r.remind_at, r.repeat) == (
        "Выпить <воды>",
        now + timedelta(minutes=2),
        Repeat.NONE,
    )
    assert f"reminder-{r.id}" in aps.jobs
    assert deps.drafts.pop((OWNER, 700), now) is None
    assert "Записал" in sent_texts(cb.message.edit_text)[0]


async def test_two_voice_drafts_are_independent(deps):
    """Голосовое А, потом Б, «Сохранить» под А → сохраняется именно А."""
    await make_draft(deps, status_id=701, task="А")
    await make_draft(deps, status_id=702, task="Б")
    await confirm(deps, "save", mid=701)
    assert [r.task for r in deps.db.list_pending(OWNER)] == ["А"]
    await confirm(deps, "save", mid=702)
    assert sorted(r.task for r in deps.db.list_pending(OWNER)) == ["А", "Б"]


async def test_cancel_discards_draft(deps, now):
    await make_draft(deps)
    await confirm(deps, "cancel")
    assert deps.db.list_pending(OWNER) == []
    assert deps.drafts.pop((OWNER, 700), now) is None


async def test_save_without_draft(deps):
    cb = await confirm(deps, "save")
    assert deps.db.list_pending(OWNER) == []
    assert "устарел" in sent_texts(cb.message.edit_text)[0]


async def test_save_after_time_passed_refused(deps, now):
    await make_draft(deps)
    deps.clock = lambda: now + timedelta(minutes=5)
    cb = await confirm(deps, "save")
    assert deps.db.list_pending(OWNER) == []
    assert "прошл" in sent_texts(cb.message.edit_text)[0]


def test_draft_store_expires(now):
    store = DraftStore(ttl=timedelta(minutes=30))
    draft = ParsedReminder("x", now + timedelta(hours=1), Repeat.NONE)
    store.put((OWNER, 1), draft, now)
    assert store.pop((OWNER, 1), now + timedelta(minutes=31)) is None
    store.put((OWNER, 2), draft, now)
    assert store.pop((OWNER, 2), now + timedelta(minutes=29)) == draft
