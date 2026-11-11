"""Точка входа: Bot + Dispatcher + фильтр владельца + роутеры + планировщик + воркер Link2Note.

Хранилище — VAULT_BACKEND: git (свой клон, GitVault) или api (сайт Second Brain через офлайн-
очередь, QueuedVault + Outbox). DRY_RUN — без Telegram: база, очередь сайта и heartbeat.
"""

import asyncio
import contextlib
import logging
import os
import sys
from datetime import datetime, timedelta, tzinfo

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import Settings, gemini_models_log, get_settings
from database import Database
from digest.fetchers import VideoOptions, cookies_warning, ffmpeg_status, find_ffmpeg
from digest.llm import DigestLLM
from digest.outbox import Outbox, QueuedVault
from digest.vault import GitVault
from digest.vault_api import ApiVault
from digest.vault_base import VaultBackend
from digest.weekly import schedule_weekly
from digest.worker import Job, Postponer, restore_jobs, run_worker
from handlers import basic, brain, context, links, notes, voice
from handlers import reminders as reminders_handlers
from handlers.middlewares import setup_owner_filter
from scheduler import ReminderScheduler
from services.context import ContextMemory
from services.drafts import DraftStore
from services.heartbeat import run_heartbeat
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
    vault: VaultBackend | None = None,
    outbox: Outbox | None = None,
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
        vault=vault,
        brain=vault if isinstance(vault, QueuedVault) else None,
        outbox=outbox,
    )
    setup_owner_filter(dp, owner_id)
    # Порядок важен: команды → команды сайта (/task …https:// не должна уйти в ссылки) → FSM
    # напоминаний → голос → ссылки → кнопки конспектов и /unread → «текст как контекст» последним.
    dp.include_routers(
        basic.router,
        brain.router,
        reminders_handlers.router,
        voice.router,
        links.router,
        notes.router,
        context.router,
    )
    return dp


def build_vault(
    settings: Settings, db: Database, *, bot=None
) -> tuple[VaultBackend | None, Outbox | None]:
    """git → GitVault (None, если VAULT_REPO не клон); api → QueuedVault поверх Outbox.

    В режиме api сайт может быть выключен при старте — это нормально: записи ждут в очереди.
    """
    if settings.vault_backend == "api":
        api = ApiVault(
            settings.second_brain_api_url,
            settings.second_brain_api_token,
            settings.vault_folder,
            timeout_s=settings.second_brain_timeout_s,
        )
        outbox = Outbox(
            db,
            api,
            bot=bot,
            owner_id=settings.owner_id,
            max_age=settings.outbox_max_age,
            max_attempts=settings.outbox_max_attempts,
        )
        return QueuedVault(outbox, api), outbox
    if not settings.vault_repo:
        return None, None
    vault = GitVault(
        settings.vault_repo,
        settings.vault_folder,
        settings.git_author_name,
        settings.git_author_email,
    )
    if not vault.is_ready():
        log.warning("VAULT_REPO=%s — не git-репозиторий, ссылки не принимаются", vault.repo)
        return None, None
    return vault, None


def build_llms(settings: Settings) -> tuple[DigestLLM, DigestLLM | None]:
    """Основная модель конспектов и модель для «🔁 Подробнее» (её запасная — основная)."""
    llm = DigestLLM(
        settings.gemini_api_key,
        settings.gemini_model,
        settings.gemini_timeout_s,
        fallback_model=settings.gemini_fallback_model,
    )
    detail_model = settings.gemini_detail_model.strip()
    if not detail_model or detail_model == settings.gemini_model:
        return llm, None
    detail = DigestLLM(
        settings.gemini_api_key,
        detail_model,
        settings.gemini_timeout_s,
        fallback_model=settings.gemini_model,
    )
    return llm, detail


def _log_queue(outbox: Outbox | None) -> None:
    if outbox is None:
        return
    counts = outbox.db.count_ops()
    log.info(
        "Очередь сайта Second Brain: ждут %d, не отправлены (dead) %d",
        counts["pending"],
        counts["dead"],
    )


class _LogBot:
    """DRY_RUN: вместо Telegram — строки в лог (outbox уведомляет через бота)."""

    async def send_message(self, chat_id=None, text="", **_):
        log.info("DRY_RUN → %s: %s", chat_id, text)

    async def edit_message_text(self, text="", chat_id=None, message_id=None, **_):
        log.info("DRY_RUN ✎ %s/%s: %s", chat_id, message_id, text)


async def _dry_run(settings: Settings, db: Database) -> None:
    """Без Telegram и Gemini: проверить, что контейнер живой (heartbeat, база, очередь сайта)."""
    log.warning("DRY_RUN=1: Telegram не подключается, ссылки не принимаются")
    vault, outbox = build_vault(settings, db, bot=_LogBot())
    log.info("Хранилище: %s", settings.vault_backend if vault is not None else "не настроено")
    _log_queue(outbox)
    tasks = [asyncio.create_task(run_heartbeat(settings.db_path.parent / "heartbeat"))]
    if outbox is not None:
        tasks.append(asyncio.create_task(outbox.run_forever()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()


async def _stop(*tasks: asyncio.Task | None) -> None:
    for task in tasks:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def main() -> None:
    settings = get_settings()
    tz = settings.timezone
    for i, line in enumerate(gemini_models_log(settings, os.environ)):
        (log.info if i == 0 else log.warning)(line)
    if settings.gemini_detail_model:
        log.info("Gemini для «Подробнее»: %s", settings.gemini_detail_model)
    log.info(ffmpeg_status(find_ffmpeg()))

    db = Database(settings.db_path)
    db.init()
    if settings.dry_run:
        await _dry_run(settings, db)
        return

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    vault, outbox = build_vault(settings, db, bot=bot)
    log.info("Хранилище: VAULT_BACKEND=%s", settings.vault_backend)
    _log_queue(outbox)
    link_queue: asyncio.Queue[Job] = asyncio.Queue()

    scheduler = ReminderScheduler(bot, db, tz)
    # Один Whisper на голосовые и запасной путь конспектов (модель грузится один раз).
    transcriber = Transcriber(
        settings.whisper_model, settings.whisper_device, settings.whisper_compute_type
    )
    dp = build_dispatcher(
        owner_id=settings.owner_id,
        db=db,
        reminders=scheduler,
        transcriber=transcriber,
        intent=IntentParser(
            settings.gemini_api_key,
            settings.gemini_model,
            tz,
            fallback_model=settings.gemini_fallback_model,
        ),
        memory=ContextMemory(ttl=timedelta(minutes=15)),
        tz=tz,
        voice_max_seconds=settings.voice_max_seconds,
        link_queue=link_queue,
        vault_ready=vault is not None,
        vault=vault,
        outbox=outbox,
    )

    clock = lambda: datetime.now(tz)  # noqa: E731
    postponer = Postponer(link_queue)
    heartbeat = asyncio.create_task(run_heartbeat(settings.db_path.parent / "heartbeat"))
    sender = asyncio.create_task(outbox.run_forever()) if outbox is not None else None

    async with httpx.AsyncClient(timeout=15) as http:
        worker = None
        if vault is not None:
            if warning := cookies_warning(settings.cookies_file):
                log.warning(warning)
            restored = await restore_jobs(db, link_queue, bot, postponer=postponer, clock=clock)
            if restored:
                log.info("Очередь Link2Note: после перезапуска вернул задач: %d", restored)
            llm, detail_llm = build_llms(settings)
            worker = asyncio.create_task(
                run_worker(
                    link_queue,
                    bot=bot,
                    llm=llm,
                    detail_llm=detail_llm,
                    http=http,
                    vault=vault,
                    outbox=outbox,
                    today=lambda: datetime.now(tz).date(),
                    db=db,
                    video=VideoOptions(
                        settings.cookies_file, settings.video_max_seconds, settings.video_max_mb
                    ),
                    clock=clock,
                    postponer=postponer,
                    transcriber=transcriber,
                    whisper_max_seconds=settings.whisper_fallback_max_seconds,
                )
            )
            if settings.weekly_digest:
                schedule_weekly(
                    scheduler.scheduler, bot=bot, vault=vault, chat_id=settings.owner_id
                )
        try:
            await scheduler.start()
            await bot.set_my_commands(basic.COMMANDS)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            await _stop(worker)
            # После воркера: он мог успеть отложить задачу. Отложенные остаются в базе.
            postponer.close()
            await _stop(sender, heartbeat)  # ждущие операции сайта остаются в vault_ops


def run() -> None:
    """Ctrl+C: воркер отменён в finally (задача остаётся в очереди) — выходим без трейсбека."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    run()
