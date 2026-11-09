"""Callback-данные и клавиатуры — одно место для всех роутеров и планировщика."""

from datetime import tzinfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from services.timeparse import Repeat

CREATE_BUTTON = "➕ Создать напоминание"
LIST_BUTTON = "📋 Мои напоминания"


class ReminderCb(CallbackData, prefix="rem"):
    action: str  # open | edit | delete | list
    id: int = 0


class RepeatCb(CallbackData, prefix="rep"):
    value: str  # Repeat


class SnoozeCb(CallbackData, prefix="snz"):
    action: str  # m15 | h1 | tmr | done
    id: int


class VoiceCb(CallbackData, prefix="voice"):
    action: str  # save | cancel
    mid: int  # message_id сообщения с черновиком


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CREATE_BUTTON), KeyboardButton(text=LIST_BUTTON)]],
        resize_keyboard=True,
    )


def _button(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def reminders_list(reminders, tz: tzinfo) -> InlineKeyboardMarkup:
    rows = []
    for r in reminders:
        mark = "🔁 " if r.repeat is not Repeat.NONE else ""
        when = r.remind_at.astimezone(tz).strftime("%d.%m %H:%M")
        task = r.task if len(r.task) <= 40 else r.task[:39] + "…"
        rows.append([_button(f"{mark}{when} — {task}", ReminderCb(action="open", id=r.id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_card(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("✏️ Изменить время", ReminderCb(action="edit", id=reminder_id)),
                _button("🗑 Удалить", ReminderCb(action="delete", id=reminder_id)),
            ],
            [_button("⬅️ Назад", ReminderCb(action="list"))],
        ]
    )


def repeat_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Один раз", RepeatCb(value=Repeat.NONE))],
            [
                _button("🔁 Каждый день", RepeatCb(value=Repeat.DAILY)),
                _button("🔁 Каждую неделю", RepeatCb(value=Repeat.WEEKLY)),
            ],
        ]
    )


def snooze(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("⏰ +15 мин", SnoozeCb(action="m15", id=reminder_id)),
                _button("+1 ч", SnoozeCb(action="h1", id=reminder_id)),
                _button("Завтра 9:00", SnoozeCb(action="tmr", id=reminder_id)),
            ],
            [_button("✅ Готово", SnoozeCb(action="done", id=reminder_id))],
        ]
    )


def voice_confirm(mid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("✅ Сохранить", VoiceCb(action="save", mid=mid)),
                _button("❌ Отмена", VoiceCb(action="cancel", mid=mid)),
            ]
        ]
    )


# --- Link2Note: кнопки под конспектом (этап 3) --------------------------------------------


class NoteCb(CallbackData, prefix="note"):
    action: str  # more | read | del | del_yes | del_no
    id: int  # notes.id


def note_actions(
    note_id: int, *, detailed: bool = False, read: bool = False
) -> InlineKeyboardMarkup:
    """«Подробнее» — один раз на заметку, «Прочитано» — пока не отмечена."""
    top = []
    if not detailed:
        top.append(_button("🔁 Подробнее", NoteCb(action="more", id=note_id)))
    if not read:
        top.append(_button("✅ Прочитано", NoteCb(action="read", id=note_id)))
    rows = [top] if top else []
    rows.append([_button("🗑 Удалить", NoteCb(action="del", id=note_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def note_delete_confirm(note_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("✅ Да, удалить", NoteCb(action="del_yes", id=note_id)),
                _button("↩️ Нет", NoteCb(action="del_no", id=note_id)),
            ]
        ]
    )
