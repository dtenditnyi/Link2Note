"""Точка входа: Bot + Dispatcher + фильтр владельца + роутеры + планировщик + воркер Link2Note."""

import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timedelta, tzinfo

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import get_settings
from database import Database
from digest.llm import DigestLLM
from digest.vault import Vault
from digest.worker import Job, run_worker
from handlers import basic, context, links, voice
from handlers import reminders as reminders_handlers
from handlers.middlewares import setup_owner_filter
from scheduler import ReminderScheduler
from services.context import ContextMemory
from services.drafts import DraftStore
from services.intent import IntentParser
from services.transcribe import Transcriber

log = logging.getLogger(__name__)


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
    link_queue: asyncio.Queue[Job],
    vault_ready: bool,
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
        link_queue=link_queue,
        vault_ready=vault_ready,
    )
    setup_owner_filter(dp, owner_id)
    # Порядок важен: команды → FSM напоминаний → голос → ссылки → «текст как контекст» последним.
    dp.include_routers(
        basic.router, reminders_handlers.router, voice.router, links.router, context.router
    )
    return dp


async def main() -> None:
    settings = get_settings()
    tz = settings.timezone

    db = Database(settings.db_path)
    db.init()

    vault = None
    if settings.vault_repo:
        vault = Vault(
            settings.vault_repo,
            settings.vault_folder,
            settings.git_author_name,
            settings.git_author_email,
        )
        if not vault.is_ready():
            log.warning("VAULT_REPO=%s — не git-репозиторий, ссылки не принимаются", vault.repo)
            vault = None
    link_queue: asyncio.Queue[Job] = asyncio.Queue()

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
        link_queue=link_queue,
        vault_ready=vault is not None,
    )

    async with httpx.AsyncClient(timeout=15) as http:
        worker = None
        if vault is not None:
            worker = asyncio.create_task(
                run_worker(
                    link_queue,
                    bot=bot,
                    llm=DigestLLM(
                        settings.gemini_api_key, settings.gemini_model, settings.gemini_timeout_s
                    ),
                    http=http,
                    vault=vault,
                    today=lambda: datetime.now(tz).date(),
                )
            )
        try:
            await scheduler.start()
            await bot.set_my_commands(basic.COMMANDS)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            if worker is not None:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
