"""Голосовое → Whisper → Gemini → черновик напоминания → «✅ Сохранить» / «❌ Отмена».

Если Gemini недоступен (503/429/таймаут после ретраев и запасной модели) — разбираем текст
нашим парсером времени; не вышло — показываем распознанный текст и кнопку «🔁 Повторить».
"""

import logging
import tempfile
from collections.abc import Callable
from datetime import datetime, tzinfo
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from database import Database
from handlers import keyboards as kb
from handlers.keyboards import VoiceCb, VoiceRetryCb
from handlers.reminders import RETRY_STAGE_TEXT, card_text
from handlers.utils import edit_text
from scheduler import ReminderScheduler
from services.context import ContextMemory
from services.drafts import DraftStore, TranscriptStore
from services.intent import IntentError, IntentParser, LLMUnavailableError, ParsedReminder
from services.text_reminder import parse_text_reminder
from services.timeparse import REPEAT_LABELS, Repeat, format_dt, next_occurrence
from services.transcribe import Transcriber

__all__ = ["VoiceCb", "VoiceRetryCb", "router"]

log = logging.getLogger(__name__)
router = Router(name="voice")


def _local_parse(text: str, now: datetime) -> ParsedReminder | None:
    """Наш парсер времени (без Gemini): только если нашлись и время, и задача."""
    try:
        parsed = parse_text_reminder(text, now)
    except ValueError:
        return None
    if parsed.remind_at is None or not parsed.task:
        return None
    return ParsedReminder(parsed.task, parsed.remind_at, Repeat.NONE)


async def _process_text(
    status,
    chat_id: int,
    text: str,
    *,
    drafts: DraftStore,
    transcripts: TranscriptStore,
    intent: IntentParser,
    memory: ContextMemory,
    clock: Callable[[], datetime],
    tz: tzinfo,
) -> None:
    """Разбор распознанного текста → подтверждение, или «Я услышал» + «Повторить»."""
    key = (chat_id, status.message_id)
    await status.edit_text(f"<i>🤔 Разбираю: «{escape(text)}»</i>")
    now = clock()
    context = memory.get(chat_id, now)

    async def on_retry(stage: str) -> None:
        await status.edit_text(RETRY_STAGE_TEXT[stage])

    note = ""
    try:
        parsed = await intent.parse(text, context, now, on_retry=on_retry)
    except IntentError as err:
        await status.edit_text(f"Не понял напоминание: {escape(str(err))}")
        return
    except LLMUnavailableError as err:
        parsed = _local_parse(text, clock())
        if parsed is None:
            transcripts.put(key, text, now)
            await status.edit_text(
                f"🎧 Я услышал: «{escape(text)}»\n\n"
                f"Gemini сейчас недоступен ({escape(str(err))}), а время в тексте я не нашёл.\n"
                "Нажми «🔁 Повторить» или напиши текстом, например: "
                "«через 20 мин напомни выпить воды».",
                reply_markup=kb.voice_retry(status.message_id),
            )
            return
        note = "\n<i>(разобрал без Gemini — он сейчас недоступен)</i>"
    except Exception:
        log.exception("Voice text parsing failed")
        await status.edit_text("Не удалось разобрать голосовое. Попробуй ещё раз.")
        return
    else:
        memory.pop(chat_id, now)  # контекст израсходован только при успешном разборе Gemini

    transcripts.pop(key, now)
    drafts.put(key, parsed, now)
    await status.edit_text(
        "Понял так:\n"
        f"<b>Что:</b> {escape(parsed.task)}\n"
        f"<b>Когда:</b> {format_dt(parsed.remind_at, tz)}\n"
        f"<b>Повтор:</b> {REPEAT_LABELS[parsed.repeat]}{note}\n\n"
        "Сохранить?",
        reply_markup=kb.voice_confirm(status.message_id),
    )


@router.message(F.voice)
async def on_voice(
    message: Message,
    bot: Bot,
    drafts: DraftStore,
    transcripts: TranscriptStore,
    transcriber: Transcriber,
    intent: IntentParser,
    memory: ContextMemory,
    clock: Callable[[], datetime],
    tz: tzinfo,
    voice_max_seconds: int,
) -> None:
    if (message.voice.duration or 0) > voice_max_seconds:
        await message.answer(f"Голосовое длиннее {voice_max_seconds} с — запиши покороче.")
        return

    status = await message.answer("<i>🎧 Слушаю…</i>")
    try:
        with tempfile.TemporaryDirectory(prefix="l2n_voice_") as tmp:
            path = Path(tmp) / f"{message.voice.file_unique_id}.ogg"
            await bot.download(message.voice, destination=path)
            text = await transcriber.transcribe(path)
    except Exception:
        log.exception("Voice transcription failed")
        await status.edit_text("Не удалось обработать голосовое. Попробуй ещё раз.")
        return
    if not text.strip():
        await status.edit_text("Не расслышал — попробуй ещё раз.")
        return
    await _process_text(
        status,
        message.chat.id,
        text,
        drafts=drafts,
        transcripts=transcripts,
        intent=intent,
        memory=memory,
        clock=clock,
        tz=tz,
    )


@router.callback_query(VoiceRetryCb.filter())
async def on_voice_retry(
    cb: CallbackQuery,
    callback_data: VoiceRetryCb,
    drafts: DraftStore,
    transcripts: TranscriptStore,
    intent: IntentParser,
    memory: ContextMemory,
    clock: Callable[[], datetime],
    tz: tzinfo,
) -> None:
    """«🔁 Повторить»: тот же распознанный текст снова в разбор — без скачивания и Whisper."""
    if cb.message is None:
        await cb.answer("Сообщение недоступно — отправь голосовое ещё раз.", show_alert=True)
        return
    chat_id = cb.message.chat.id
    text = transcripts.get((chat_id, callback_data.mid), clock())
    if text is None:
        await edit_text(cb, "Текст устарел — отправь голосовое ещё раз.")
        await cb.answer()
        return
    await cb.answer()
    status = cb.message
    if isinstance(status, InaccessibleMessage):  # старое сообщение — продолжаем в новом
        transcripts.pop((chat_id, callback_data.mid), clock())
        status = await cb.bot.send_message(chat_id, "<i>🔁 Повторяю…</i>")
    await _process_text(
        status,
        chat_id,
        text,
        drafts=drafts,
        transcripts=transcripts,
        intent=intent,
        memory=memory,
        clock=clock,
        tz=tz,
    )


@router.callback_query(VoiceCb.filter())
async def on_voice_confirm(
    cb: CallbackQuery,
    callback_data: VoiceCb,
    drafts: DraftStore,
    db: Database,
    reminders: ReminderScheduler,
    tz: tzinfo,
    clock: Callable[[], datetime],
) -> None:
    chat_id = cb.message.chat.id
    now = clock()
    draft = drafts.pop((chat_id, callback_data.mid), now)

    if callback_data.action == "cancel":
        await edit_text(cb, "❌ Отменено.")
        await cb.answer()
        return
    if draft is None:
        await edit_text(cb, "Черновик устарел — отправь голосовое ещё раз.")
        await cb.answer()
        return

    remind_at = draft.remind_at
    if remind_at <= now:
        if draft.repeat is Repeat.NONE:
            await edit_text(cb, "Время уже прошло — отправь голосовое ещё раз.")
            await cb.answer()
            return
        remind_at = next_occurrence(remind_at, draft.repeat, now, tz)

    rid = db.add(chat_id, draft.task, remind_at, draft.repeat)
    reminder = db.get(rid, chat_id)
    reminders.schedule(reminder)
    await edit_text(cb, "✅ Записал!\n" + card_text(reminder, tz))
    await cb.answer()
