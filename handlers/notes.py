"""Кнопки под конспектом (🔁 Подробнее / ✅ Прочитано / 🗑 Удалить) и /unread.

Git-операции идут секунды, поэтому на нажатие отвечаем сразу, а результат показываем правкой
клавиатуры или ответом. «Подробнее» — задача в общую очередь ссылок (тратит 1 запрос Gemini).
"""

import asyncio
import logging
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message

from database import Database, NoteRow
from digest.errors import NoteNotFound, VaultError, VaultQueued
from digest.vault_base import VaultBackend
from digest.weekly import unread_text
from digest.worker import Job
from handlers import keyboards as kb
from handlers.keyboards import NoteCb
from handlers.utils import edit_text

log = logging.getLogger(__name__)
router = Router(name="notes")

DETAILED_HINT = (
    "Сделай максимально подробный конспект: все шаги, команды, горячие клавиши и ресурсы, "
    "7–15 главных мыслей, больше таймкодов."
)
NO_VAULT = "Хранилище не настроено (VAULT_REPO)."
NOT_FOUND = "Заметка не найдена — возможно, уже удалена."
ALL_READ = "Всё прочитано 🎉"


def _actions(note: NoteRow, **override: bool) -> InlineKeyboardMarkup:
    flags = {"detailed": note.detailed, "read": note.read} | override
    return kb.note_actions(note.id, **flags)


async def _set_markup(cb: CallbackQuery, markup: InlineKeyboardMarkup) -> None:
    """Кнопки жмут и через неделю: старое сообщение (InaccessibleMessage) правим по id."""
    msg, bot = cb.message, cb.bot
    if msg is None or bot is None:  # кнопка из inline-сообщения — у нас таких нет
        return
    try:
        if isinstance(msg, InaccessibleMessage):
            await bot.edit_message_reply_markup(
                chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=markup
            )
        else:
            await msg.edit_reply_markup(reply_markup=markup)
    except Exception:
        log.exception("Не смог обновить кнопки под конспектом")


async def _reply(cb: CallbackQuery, text: str) -> Message:
    msg, bot = cb.message, cb.bot
    if msg is None or bot is None:
        raise RuntimeError("callback без сообщения")
    if isinstance(msg, InaccessibleMessage):
        return await bot.send_message(msg.chat.id, text)
    return await msg.reply(text)


@router.callback_query(NoteCb.filter())
async def on_note(
    cb: CallbackQuery,
    callback_data: NoteCb,
    db: Database,
    vault: VaultBackend | None,
    link_queue: asyncio.Queue[Job],
) -> None:
    if cb.message is None:  # кнопка из inline-сообщения — у нас таких нет
        await cb.answer()
        return
    if vault is None:
        await cb.answer(NO_VAULT, show_alert=True)
        return
    note = db.get_note(callback_data.id)
    if note is None:
        await cb.answer(NOT_FOUND, show_alert=True)
        return
    action = callback_data.action
    chat_id = cb.message.chat.id

    if action == "del":
        await cb.answer()
        await _set_markup(cb, kb.note_delete_confirm(note.id))
    elif action == "del_no":
        await cb.answer()
        await _set_markup(cb, _actions(note))
    elif action == "more":
        if not db.mark_detailed(note.id):
            await cb.answer("Подробный конспект уже сделан или уже в очереди.")
            return
        await cb.answer("🔁 Делаю подробнее")
        try:
            status = await _reply(cb, "⏳ Подробный конспект — в очереди…")
            job_id = db.add_job(
                note.url, DETAILED_HINT, True, chat_id, status.message_id, note_id=note.id
            )
        except Exception:
            db.mark_detailed(note.id, False)  # задачи нет — кнопка должна работать снова
            raise
        link_queue.put_nowait(
            Job(
                url=note.url,
                hint=DETAILED_HINT,
                chat_id=chat_id,
                status_msg_id=status.message_id,
                force=True,
                id=job_id,
                note_id=note.id,
            )
        )
        await _set_markup(cb, _actions(note, detailed=True))
    elif action == "read":
        await cb.answer("⏳ Отмечаю…")
        await _read(cb, note, db, vault)
    elif action == "del_yes":
        await cb.answer("⏳ Удаляю…")
        await _delete(cb, note, db, vault)
    else:
        await cb.answer()


async def _read(cb: CallbackQuery, note: NoteRow, db: Database, vault: VaultBackend) -> None:
    try:
        name = await vault.set_status(
            note.note_name, note.url, "read", skip=db.other_note_names(note.id)
        )
    except NoteNotFound as err:
        await _reply(cb, f"❌ {escape(str(err))}")
        return
    except VaultError as err:
        if not err.note_name:  # не закоммитили — ничего не изменилось
            await _reply(cb, f"❌ {escape(str(err))}")
            return
        name = err.note_name  # изменена локально / ждёт в очереди сайта — уйдёт сама
        await _reply(cb, _later(err))
    db.mark_read(note.id)
    if name != note.note_name:
        db.rename_note(note.id, name)
    await _set_markup(cb, _actions(note, read=True))


async def _delete(cb: CallbackQuery, note: NoteRow, db: Database, vault: VaultBackend) -> None:
    try:
        await vault.delete(note.note_name, note.url, skip=db.other_note_names(note.id))
        text = f"🗑 Удалено: «{escape(note.note_name)}»"
    except NoteNotFound:
        text = f"🗑 Файла «{escape(note.note_name)}» уже нет в хранилище — убрал из базы."
    except VaultError as err:
        if not err.note_name:
            await _reply(cb, f"❌ {escape(str(err))}")
            return
        text = f"🗑 Удалено: «{escape(note.note_name)}»\n{_later(err)}"
    db.delete_note(note.id)
    await edit_text(cb, text)


def _later(err: VaultError) -> str:
    """git: «⚠️ не отправлена в GitHub…»; api: «📮 Сайт недоступен…» (эмодзи уже в тексте)."""
    return escape(str(err)) if isinstance(err, VaultQueued) else f"⚠️ {escape(str(err))}"


@router.message(Command("unread"))
async def cmd_unread(message: Message, vault: VaultBackend | None) -> None:
    if vault is None:
        await message.answer(NO_VAULT)
        return
    try:
        text = unread_text(await vault.unread())
    except Exception:
        log.exception("/unread не удался")
        await message.answer("Не смог прочитать хранилище — подробности в логах.")
        return
    await message.answer(text or ALL_READ)
