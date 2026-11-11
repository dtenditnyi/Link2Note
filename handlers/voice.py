"""Голосовое → Whisper → Gemini → черновик напоминания → «✅ Сохранить» / «❌ Отмена»."""

import logging
import tempfile
from collections.abc import Callable
from datetime import datetime, tzinfo
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from database import Database
from digest.outbox import QueuedVault
from handlers import keyboards as kb
from handlers.brain import add_note, add_task
from handlers.keyboards import VoiceCb, VoiceRetryCb
from handlers.reminders import card_text
from handlers.utils import edit_text
from scheduler import ReminderScheduler
from services.brain_intent import voice_command
from services.context import ContextMemory
from services.drafts import DraftStore, TextStore
from services.gemini_errors import is_overloaded
from services.intent import IntentError, IntentParser, ParsedReminder
from services.text_reminder import parse_text_reminder
from services.timeparse import REPEAT_LABELS, Repeat, format_dt, next_occurrence
from services.transcribe import Transcriber

__all__ = ["VoiceCb", "VoiceRetryCb", "router"]

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
    brain: QueuedVault | None = None,
    transcripts: TextStore | None = None,
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
        if brain is not None and (command := voice_command(text)) is not None:
            kind, rest = command  # «добавь задачу …» / «запиши в заметки …» — без Gemini (D11)
            await status.edit_text(f"<i>🎧 «{escape(text)}»</i>")
            if kind == "task":
                await add_task(message, brain, rest, clock())
            else:
                await add_note(message, brain, rest)
            return
    except Exception:
        log.exception("Voice processing failed")
        await status.edit_text("Не удалось обработать голосовое. Попробуй ещё раз.")
        return
    await _understand(
        status, message.chat.id, text,
        drafts=drafts, intent=intent, memory=memory, clock=clock, tz=tz, transcripts=transcripts,
    )  # fmt: skip


async def _understand(
    status,
    chat_id: int,
    text: str,
    *,
    drafts: DraftStore,
    intent: IntentParser,
    memory: ContextMemory,
    clock: Callable[[], datetime],
    tz: tzinfo,
    transcripts: TextStore | None,
) -> None:
    """Текст голосового → черновик напоминания. Gemini перегружен (429/5xx после запасной
    модели) → наш парсер времени; не вышло — расшифровка не теряется: «🔁 Повторить»."""
    await status.edit_text(f"<i>🤔 Разбираю: «{escape(text)}»</i>")
    now = clock()
    note = ""
    try:
        context = memory.get(chat_id, now)
        parsed = await intent.parse(text, context, now)
    except IntentError as err:
        await status.edit_text(f"Не понял напоминание: {escape(str(err))}")
        return
    except Exception as err:
        if not is_overloaded(err):
            log.exception("Voice processing failed")
            await status.edit_text("Не удалось обработать голосовое. Попробуй ещё раз.")
            return
        log.warning("Голосовое: Gemini перегружен (%s) — разбираю сам", getattr(err, "code", err))
        parsed = _local_parse(text, now)
        if parsed is None:
            await _keep_transcript(status, chat_id, text, now, transcripts)
            return
        note = "\n<i>(разобрал без Gemini — он сейчас перегружен)</i>"
    else:
        memory.pop(chat_id, now)  # контекст израсходован только при успешном разборе Gemini
    drafts.put((chat_id, status.message_id), parsed, now)
    await status.edit_text(
        "Понял так:\n"
        f"<b>Что:</b> {escape(parsed.task)}\n"
        f"<b>Когда:</b> {format_dt(parsed.remind_at, tz)}\n"
        f"<b>Повтор:</b> {REPEAT_LABELS[parsed.repeat]}{note}\n\n"
        "Сохранить?",
        reply_markup=kb.voice_confirm(status.message_id),
    )


def _local_parse(text: str, now: datetime) -> ParsedReminder | None:
    try:
        found = parse_text_reminder(text, now)
    except ValueError:  # время в прошлом
        return None
    if found.remind_at is None or not found.task:
        return None
    return ParsedReminder(found.task, found.remind_at, Repeat.NONE)


async def _keep_transcript(
    status, chat_id: int, text: str, now: datetime, transcripts: TextStore | None
) -> None:
    body = (
        f"🎧 Я услышал: «{escape(text)}»\n\n"
        "Gemini сейчас перегружен, а время я сам не нашёл. "
        "Нажми «Повторить» чуть позже или напиши текстом: «через 20 мин напомни …»."
    )
    if transcripts is None:
        await status.edit_text(body)
        return
    transcripts.put((chat_id, status.message_id), text, now)
    await status.edit_text(body, reply_markup=kb.voice_retry(status.message_id))


@router.callback_query(VoiceRetryCb.filter())
async def on_voice_text_retry(
    cb: CallbackQuery,
    callback_data: VoiceRetryCb,
    drafts: DraftStore,
    intent: IntentParser,
    memory: ContextMemory,
    clock: Callable[[], datetime],
    tz: tzinfo,
    transcripts: TextStore | None = None,
) -> None:
    """«🔁 Повторить»: тот же текст снова в Gemini — без скачивания и Whisper."""
    msg = cb.message
    if msg is None or isinstance(msg, InaccessibleMessage):
        await cb.answer("Текст устарел — отправь голосовое ещё раз.", show_alert=True)
        return
    text = transcripts.pop((msg.chat.id, callback_data.mid), clock()) if transcripts else None
    await cb.answer()
    if text is None:
        await edit_text(cb, "Текст устарел — отправь голосовое ещё раз.")
        return
    await _understand(
        msg, msg.chat.id, text,
        drafts=drafts, intent=intent, memory=memory, clock=clock, tz=tz, transcripts=transcripts,
    )  # fmt: skip


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
