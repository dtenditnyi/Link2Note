"""Правка сообщения из callback с учётом InaccessibleMessage (сообщения старше 48 ч)."""

from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup


async def edit_text(
    cb: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    if isinstance(cb.message, InaccessibleMessage):
        # Старое сообщение править нельзя — отвечаем новым.
        await cb.bot.send_message(cb.message.chat.id, text, reply_markup=reply_markup)
    else:
        await cb.message.edit_text(text, reply_markup=reply_markup)


async def remove_markup(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, InaccessibleMessage):
        await cb.message.edit_reply_markup(reply_markup=None)
