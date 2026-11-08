"""Голосовое → Whisper → Gemini → черновик напоминания → «✅ Сохранить» / «❌ Отмена»."""

import logging
import tempfile
from collections.abc import Callable
from datetime import datetime, tzinfo
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from database import Database
from handlers import keyboards as kb
from handlers.keyboards import VoiceCb
from handlers.reminders import card_text
from handlers.utils import edit_text
from scheduler import ReminderScheduler
from services.context import ContextMemory
from services.drafts import DraftStore
from services.intent import IntentError, IntentParser
from services.timeparse import REPEAT_LABELS, Repeat, format_dt, next_occurrence
from services.transcribe import Transcriber

__all__ = ["VoiceCb", "router"]

log = logging.getLogger(__name__)
router = Router(name="voice")


@router.message(F.voice)
async def on_voice(
    message: Message,
    bot: Bot,
    drafts: DraftStore,
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
        if not text.strip():
            await status.edit_text("Не расслышал — попробуй ещё раз.")
            return
        await status.edit_text(f"<i>🤔 Разбираю: «{escape(text)}»</i>")
        now = clock()
        context = memory.get(message.chat.id, now)
        parsed = await intent.parse(text, context, now)
    except IntentError as err:
        await status.edit_text(f"Не понял напоминание: {escape(str(err))}")
        return
    except Exception:
        log.exception("Voice processing failed")
        await status.edit_text("Не удалось обработать голосовое. Попробуй ещё раз.")
        return

    memory.pop(message.chat.id, now)  # контекст израсходован только при успешном разборе
    drafts.put((message.chat.id, status.message_id), parsed, now)
    await status.edit_text(
        "Понял так:\n"
        f"<b>Что:</b> {escape(parsed.task)}\n"
        f"<b>Когда:</b> {format_dt(parsed.remind_at, tz)}\n"
        f"<b>Повтор:</b> {REPEAT_LABELS[parsed.repeat]}\n\n"
        "Сохранить?",
        reply_markup=kb.voice_confirm(status.message_id),
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
