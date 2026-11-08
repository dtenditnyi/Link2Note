"""Точка входа: Bot + Dispatcher + фильтр владельца + роутеры + планировщик напоминаний."""

import asyncio
import logging
import sys
from datetime import datetime, timedelta, tzinfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import get_settings
from database import Database
from handlers import basic, context, voice
from handlers import reminders as reminders_handlers
from handlers.middlewares import setup_owner_filter
from scheduler import ReminderScheduler
from services.context import ContextMemory
from services.drafts import DraftStore
from services.intent import IntentParser
from services.transcribe import Transcriber


def build_dispatcher(
    *,
    owner_id: int,
    db: Database,
    reminders: ReminderScheduler,
    transcriber: Transcriber,
    intent: IntentParser,
    memory: ContextMemory,
    tz: tzinfo,
    voice_max_seconds: int,
) -> Dispatcher:
    dp = Dispatcher(
        db=db,
        reminders=reminders,
        transcriber=transcriber,
        intent=intent,
        memory=memory,
        drafts=DraftStore(),
        tz=tz,
        clock=lambda: datetime.now(tz),
        voice_max_seconds=voice_max_seconds,
    )
    setup_owner_filter(dp, owner_id)
    # Порядок важен: команды → FSM напоминаний → голос → «текст как контекст» последним.
    dp.include_routers(basic.router, reminders_handlers.router, voice.router, context.router)
    return dp


async def main() -> None:
    settings = get_settings()
    tz = settings.timezone

    db = Database(settings.db_path)
    db.init()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    scheduler = ReminderScheduler(bot, db, tz)
    dp = build_dispatcher(
        owner_id=settings.owner_id,
        db=db,
        reminders=scheduler,
        transcriber=Transcriber(
            settings.whisper_model, settings.whisper_device, settings.whisper_compute_type
        ),
        intent=IntentParser(settings.gemini_api_key, settings.gemini_model, tz),
        memory=ContextMemory(ttl=timedelta(minutes=15)),
        tz=tz,
        voice_max_seconds=settings.voice_max_seconds,
    )

    await scheduler.start()
    await bot.set_my_commands(basic.COMMANDS)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
