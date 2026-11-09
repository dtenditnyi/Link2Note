"""Напоминания: создание (FSM), список, карточка, изменение, удаление, отложить/готово."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database import Database, Reminder
from handlers import keyboards as kb
from handlers.keyboards import CREATE_BUTTON, LIST_BUTTON, ReminderCb, RepeatCb, SnoozeCb
from handlers.utils import edit_text, remove_markup
from scheduler import ReminderScheduler
from services.intent import IntentError, IntentParser
from services.text_reminder import has_trigger, parse_text_reminder
from services.timeparse import (
    FORMATS_HELP,
    REPEAT_LABELS,
    Repeat,
    format_dt,
    next_occurrence,
    parse_reminder_time,
)

__all__ = ["CREATE_BUTTON", "LIST_BUTTON", "ReminderCb", "RepeatCb", "SnoozeCb", "router"]

log = logging.getLogger(__name__)
router = Router(name="reminders")
FREE_TEXT_HINT = (
    "Напиши время цифрами, например «через 20 мин напомни выпить воды» "
    "или «завтра в 9:00 напомни позвонить», — или создай через /new."
)
NOT_FOUND = "Напоминание не найдено."


class CreateReminder(StatesGroup):
    task = State()
    time = State()
    repeat = State()


class EditReminder(StatesGroup):
    time = State()


def card_text(r: Reminder, tz: tzinfo) -> str:
    return (
        f"<b>Что:</b> {escape(r.task)}\n"
        f"<b>Когда:</b> {format_dt(r.remind_at, tz)}\n"
        f"<b>Повтор:</b> {REPEAT_LABELS[r.repeat]}"
    )


# --- Создание ---------------------------------------------------------------------


@router.message(Command("new"))
@router.message(F.text == CREATE_BUTTON)
async def start_create(message: Message, state: FSMContext) -> None:
    await state.set_state(CreateReminder.task)
    await message.answer("Напиши, о чём напомнить. /cancel — отмена.")


@router.message(Command("list"))
@router.message(F.text == LIST_BUTTON)
async def show_list(message: Message, db: Database, tz: tzinfo) -> None:
    reminders = db.list_pending(message.chat.id)
    if not reminders:
        await message.answer("Нет активных напоминаний.")
        return
    await message.answer("Твои напоминания:", reply_markup=kb.reminders_list(reminders, tz))


async def _ask_repeat(
    message: Message, state: FSMContext, task: str, remind_at: datetime, tz: tzinfo
) -> None:
    await state.update_data(task=task, remind_at=remind_at.isoformat())
    await state.set_state(CreateReminder.repeat)
    await message.answer(_repeat_question(task, remind_at, tz), reply_markup=kb.repeat_choice())


def _repeat_question(task: str, remind_at: datetime, tz: tzinfo) -> str:
    return f"Напомню «{escape(task)}» {format_dt(remind_at, tz)}. Повторять?"


@router.message(StateFilter(None), F.text.func(has_trigger))
async def on_free_text(
    message: Message,
    state: FSMContext,
    intent: IntentParser,
    tz: tzinfo,
    clock: Callable[[], datetime],
) -> None:
    """Свободный текст со словом «напомни»: время из текста, иначе — Gemini."""
    text = message.text or ""
    now = clock()
    try:
        parsed = parse_text_reminder(text, now)
    except ValueError as err:
        await message.answer(str(err))
        return

    if parsed.remind_at is not None:
        if not parsed.task:
            await state.update_data(remind_at=parsed.remind_at.isoformat())
            await state.set_state(CreateReminder.task)
            await message.answer(
                f"Напомню {format_dt(parsed.remind_at, tz)}. О чём напомнить? /cancel — отмена."
            )
            return
        await _ask_repeat(message, state, parsed.task, parsed.remind_at, tz)
        return

    status = await message.answer("<i>🤔 Разбираю…</i>")
    try:
        result = await intent.parse(text, None, now, kind="Текстовая команда")
    except IntentError as err:
        await status.edit_text(f"{escape(str(err))}\n\n{FREE_TEXT_HINT}")
        return
    except Exception:
        log.exception("Free-text reminder parsing failed")
        await status.edit_text(f"Не получилось разобрать напоминание.\n\n{FREE_TEXT_HINT}")
        return
    await state.update_data(task=result.task, remind_at=result.remind_at.isoformat())
    await state.set_state(CreateReminder.repeat)
    await status.edit_text(
        _repeat_question(result.task, result.remind_at, tz), reply_markup=kb.repeat_choice()
    )


@router.message(CreateReminder.task)
async def on_task(
    message: Message, state: FSMContext, tz: tzinfo, clock: Callable[[], datetime]
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши текст напоминания.")
        return
    try:
        parsed = parse_text_reminder(text, clock())
    except ValueError as err:
        await message.answer(str(err))
        return
    data = await state.get_data()
    remind_at = parsed.remind_at
    if remind_at is None and "remind_at" in data:  # время пришло раньше: «напомни через 5 минут»
        remind_at = datetime.fromisoformat(data["remind_at"])

    if not parsed.task:
        if parsed.remind_at is not None:
            await state.update_data(remind_at=parsed.remind_at.isoformat())
            await message.answer(f"Напомню {format_dt(parsed.remind_at, tz)}. О чём напомнить?")
        else:
            await message.answer("Напиши текст напоминания.")
        return
    if remind_at is not None:
        await _ask_repeat(message, state, parsed.task, remind_at, tz)
        return
    await state.update_data(task=parsed.task)
    await state.set_state(CreateReminder.time)
    await message.answer(f"Когда напомнить?\n<i>{FORMATS_HELP}</i>")


@router.message(CreateReminder.time)
async def on_time(
    message: Message, state: FSMContext, tz: tzinfo, clock: Callable[[], datetime]
) -> None:
    try:
        remind_at = parse_reminder_time(message.text or "", clock())
    except ValueError as err:
        await message.answer(str(err))
        return
    task = (await state.get_data()).get("task", "")
    await _ask_repeat(message, state, task, remind_at, tz)


@router.message(CreateReminder.repeat)
async def on_repeat_text(message: Message) -> None:
    await message.answer("Выбери вариант кнопкой выше или /cancel.")


@router.callback_query(RepeatCb.filter(), CreateReminder.repeat)
async def on_repeat(
    cb: CallbackQuery,
    callback_data: RepeatCb,
    state: FSMContext,
    db: Database,
    reminders: ReminderScheduler,
    tz: tzinfo,
) -> None:
    data = await state.get_data()
    await state.clear()
    if "task" not in data or "remind_at" not in data:
        await cb.answer("Черновик устарел — создай напоминание заново.", show_alert=True)
        return
    repeat = Repeat(callback_data.value)
    remind_at = datetime.fromisoformat(data["remind_at"])
    now = reminders.clock()
    if remind_at <= now:
        if repeat is Repeat.NONE:
            await edit_text(cb, "Это время уже прошло — создай напоминание заново.")
            await cb.answer()
            return
        remind_at = next_occurrence(remind_at, repeat, now, tz)

    rid = db.add(cb.message.chat.id, data["task"], remind_at, repeat)
    reminder = db.get(rid, cb.message.chat.id)
    reminders.schedule(reminder)
    await edit_text(cb, "✅ Напоминание создано!\n" + card_text(reminder, tz))
    await cb.answer()


@router.callback_query(RepeatCb.filter())
async def on_repeat_stale(cb: CallbackQuery) -> None:
    """Кнопка повтора от старого сценария: не трогаем текущее состояние."""
    await cb.answer("Кнопка устарела — создай напоминание заново (/new).", show_alert=True)


# --- Список и карточка ------------------------------------------------------------


@router.callback_query(ReminderCb.filter(F.action == "list"))
async def on_back(cb: CallbackQuery, db: Database, tz: tzinfo) -> None:
    reminders = db.list_pending(cb.message.chat.id)
    if reminders:
        await edit_text(cb, "Твои напоминания:", reply_markup=kb.reminders_list(reminders, tz))
    else:
        await edit_text(cb, "Нет активных напоминаний.")
    await cb.answer()


@router.callback_query(ReminderCb.filter(F.action == "open"))
async def on_open(cb: CallbackQuery, callback_data: ReminderCb, db: Database, tz: tzinfo) -> None:
    r = db.get(callback_data.id, cb.message.chat.id)
    if r is None:
        await cb.answer(NOT_FOUND, show_alert=True)
        return
    await edit_text(cb, card_text(r, tz), reply_markup=kb.reminder_card(r.id))
    await cb.answer()


# --- Изменение времени ------------------------------------------------------------


@router.callback_query(ReminderCb.filter(F.action == "edit"))
async def on_edit(
    cb: CallbackQuery, callback_data: ReminderCb, state: FSMContext, db: Database
) -> None:
    if db.get(callback_data.id, cb.message.chat.id) is None:
        await cb.answer(NOT_FOUND, show_alert=True)
        return
    await state.set_state(EditReminder.time)
    await state.update_data(reminder_id=callback_data.id)
    await cb.message.answer(f"Новое время?\n<i>{FORMATS_HELP}</i>")
    await cb.answer()


@router.message(EditReminder.time)
async def on_new_time(
    message: Message,
    state: FSMContext,
    db: Database,
    reminders: ReminderScheduler,
    tz: tzinfo,
    clock: Callable[[], datetime],
) -> None:
    rid = (await state.get_data()).get("reminder_id")
    r = db.get(rid, message.chat.id) if rid is not None else None
    if r is None:
        await state.clear()
        await message.answer(NOT_FOUND)
        return
    try:
        remind_at = parse_reminder_time(message.text or "", clock())
    except ValueError as err:
        await message.answer(str(err))
        return
    db.update_time(r.id, message.chat.id, remind_at)
    updated = db.get(r.id, message.chat.id)
    reminders.schedule(updated)
    await state.clear()
    await message.answer("✅ Время изменено.\n" + card_text(updated, tz))


# --- Удаление ---------------------------------------------------------------------


@router.callback_query(ReminderCb.filter(F.action == "delete"))
async def on_delete(
    cb: CallbackQuery, callback_data: ReminderCb, db: Database, reminders: ReminderScheduler
) -> None:
    if not db.delete(callback_data.id, cb.message.chat.id):
        await cb.answer(NOT_FOUND, show_alert=True)
        return
    reminders.unschedule(callback_data.id)
    await edit_text(cb, "🗑 Напоминание удалено.")
    await cb.answer()


# --- Отложить / Готово (кнопки на сработавшем напоминании) -----------------------


def _snooze_time(action: str, now: datetime, tz: tzinfo) -> datetime:
    if action == "m15":
        return (now.astimezone(UTC) + timedelta(minutes=15)).astimezone(tz)
    if action == "h1":
        return (now.astimezone(UTC) + timedelta(hours=1)).astimezone(tz)
    if action == "tmr":
        return datetime.combine(now.astimezone(tz).date() + timedelta(days=1), time(9, 0), tz)
    raise ValueError(action)


@router.callback_query(SnoozeCb.filter())
async def on_snooze(
    cb: CallbackQuery,
    callback_data: SnoozeCb,
    db: Database,
    reminders: ReminderScheduler,
    tz: tzinfo,
    clock: Callable[[], datetime],
) -> None:
    chat_id = cb.message.chat.id
    r = db.get(callback_data.id, chat_id)
    if r is None:
        await cb.answer(NOT_FOUND, show_alert=True)
        return

    if callback_data.action == "done":
        if r.repeat is Repeat.NONE:
            if r.status == "done":
                await cb.answer("Уже выполнено.")
                return
            reminders.unschedule(r.id)
            db.set_status(r.id, "done")
        await remove_markup(cb)
        await cb.answer("✅ Готово")
        return

    new_time = _snooze_time(callback_data.action, clock(), tz)
    if r.repeat is not Repeat.NONE:
        # Серия не сдвигается — откладываем только это срабатывание разовой копией.
        copy_id = db.add(chat_id, r.task, new_time, Repeat.NONE)
        reminders.schedule(db.get(copy_id, chat_id))
    else:
        if r.status not in ("sent", "missed"):
            await cb.answer("Это напоминание ещё не сработало.", show_alert=True)
            return
        db.update_time(r.id, chat_id, new_time)
        reminders.schedule(db.get(r.id, chat_id))
    await edit_text(cb, f"{escape(r.task)}\n⏰ Отложено до {format_dt(new_time, tz)}")
    await cb.answer()
