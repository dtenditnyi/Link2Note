"""/start, /help, /cancel."""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, Message

from handlers.keyboards import main_menu
from services.timeparse import FORMATS_HELP

router = Router(name="basic")

COMMANDS = [
    BotCommand(command="new", description="Создать напоминание"),
    BotCommand(command="list", description="Мои напоминания"),
    BotCommand(command="unread", description="Непрочитанные конспекты"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
    BotCommand(command="help", description="Что умеет бот"),
]

HELP = (
    "<b>Что я умею</b>\n"
    "• 📥 Ссылка на YouTube, Reels, TikTok, X или статью (можно с пожеланием: "
    "«упор на хоткеи») — сделаю конспект и положу заметку в Obsidian. "
    "Повторная ссылка — «уже есть»; слово «заново» — сделать ещё раз\n"
    "• Под конспектом: 🔁 Подробнее · ✅ Прочитано · 🗑 Удалить\n"
    "• /unread — непрочитанные конспекты (а по воскресеньям в 18:00 пришлю сам)\n"
    "• /new — напоминание: текст → время → повтор\n"
    "• Или сразу текстом: «через 2 минуты напомни про лимиты» — время пойму сам\n"
    "• /list — активные напоминания: изменить, удалить\n"
    "• 🎙 Голосовое: «напомни завтра в 9 позвонить маме» — покажу, что понял, и сохраню\n"
    "• Текст перед голосовым — контекст («это» в голосовом = этот текст, 15 мин)\n"
    "• На сработавшем напоминании — отложить или отметить «Готово»\n\n"
    f"<i>{FORMATS_HELP}</i>"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"Привет, <b>{escape(message.from_user.full_name)}</b>!\n\n{HELP}",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=main_menu())


@router.message(Command("cancel"))
@router.message(F.text.lower().in_({"отмена", "cancel"}))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())
