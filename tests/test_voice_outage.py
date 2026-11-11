"""G-0 (перенос из feature/gemini-resilience): голосовое при перегрузке Gemini не теряется."""

from datetime import timedelta
from unittest.mock import AsyncMock

from google.genai import errors

from handlers import voice as v
from handlers.keyboards import VoiceRetryCb
from services.drafts import TextStore
from services.intent import IntentError, ParsedReminder
from services.timeparse import Repeat
from tests.conftest import TZ, make_callback
from tests.test_voice import deps, voice_message  # noqa: F401  (фикстура)


def _busy(code=503):
    return errors.ServerError(code, {"error": {"code": code, "message": "high demand"}})


async def _voice(d, transcripts, text):
    d.transcriber.transcribe = AsyncMock(return_value=text)
    msg, status = voice_message()
    await v.on_voice(msg, bot=d.bot, drafts=d.drafts, transcriber=d.transcriber, intent=d.intent,
                     memory=d.memory, clock=d.clock, tz=TZ, voice_max_seconds=120,
                     transcripts=transcripts)  # fmt: skip
    return status


def _last(status):
    call = status.edit_text.await_args
    return call.args[0], call.kwargs.get("reply_markup")


async def test_overloaded_local_parser_finds_time(deps, now):  # noqa: F811
    deps.intent.parse = AsyncMock(side_effect=_busy())
    status = await _voice(deps, TextStore(), "напомни через 2 минуты выпить воды")
    text, markup = _last(status)
    assert "разобрал без Gemini" in text and markup is not None
    draft = deps.drafts.pop((111, 700), now)
    assert draft.task == "выпить воды" and draft.remind_at == now + timedelta(minutes=2)


async def test_overloaded_no_time_keeps_transcript_with_retry(deps, now):  # noqa: F811
    deps.intent.parse = AsyncMock(side_effect=_busy(429))
    store = TextStore()
    status = await _voice(deps, store, "напомни купить <хлеб> в пятницу")
    text, markup = _last(status)
    assert "Я услышал" in text and "&lt;хлеб&gt;" in text
    assert VoiceRetryCb.unpack(markup.inline_keyboard[0][0].callback_data).mid == 700
    assert deps.db.list_pending(111) == []

    # «Повторить»: Gemini ожил — тот же текст, без скачивания и Whisper
    deps.intent.parse = AsyncMock(
        return_value=ParsedReminder("Купить хлеб", now + timedelta(days=2), Repeat.NONE)
    )
    downloads = deps.bot.download.await_count
    cb = make_callback()
    cb.message.message_id = 700
    await v.on_voice_text_retry(cb, VoiceRetryCb(mid=700), drafts=deps.drafts, intent=deps.intent,
                                memory=deps.memory, clock=deps.clock, tz=TZ,
                                transcripts=store)  # fmt: skip
    deps.intent.parse.assert_awaited_once()
    assert deps.intent.parse.await_args.args[0] == "напомни купить <хлеб> в пятницу"
    assert deps.bot.download.await_count == downloads
    deps.transcriber.transcribe.assert_awaited_once()
    assert "Понял так" in cb.message.edit_text.await_args.args[0]


async def test_retry_stale_text(deps):  # noqa: F811
    cb = make_callback()
    await v.on_voice_text_retry(cb, VoiceRetryCb(mid=1), drafts=deps.drafts, intent=deps.intent,
                                memory=deps.memory, clock=deps.clock, tz=TZ,
                                transcripts=TextStore())  # fmt: skip
    deps.intent.parse.assert_not_awaited()
    assert "устарел" in cb.message.edit_text.await_args.args[0]


async def test_not_overloaded_errors_unchanged(deps):  # noqa: F811
    deps.intent.parse = AsyncMock(side_effect=errors.ClientError(400, {"error": {}}))
    status = await _voice(deps, TextStore(), "напомни что-то")
    assert _last(status)[0] == "Не удалось обработать голосовое. Попробуй ещё раз."
    deps.intent.parse = AsyncMock(side_effect=IntentError("непонятно"))
    status = await _voice(deps, TextStore(), "напомни что-то")
    assert _last(status)[0] == "Не понял напоминание: непонятно"


def test_text_store_ttl(now):
    s = TextStore(ttl=timedelta(hours=6))
    s.put((1, 2), "x", now)
    assert s.pop((1, 2), now + timedelta(hours=7)) is None
    s.put((1, 2), "x", now)
    assert s.pop((1, 2), now) == "x" and s.pop((1, 2), now) is None
