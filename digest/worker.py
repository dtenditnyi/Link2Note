"""Очередь ссылок: задачи обрабатываются по одной, воркер никогда не падает.

Задачи лежат в SQLite (link_jobs): после перезапуска restore_jobs возвращает недоделанные
в очередь. Задача, на которой бот падал MAX_ATTEMPTS раз, больше не повторяется.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Protocol

import httpx

from database import Database
from digest.classifier import classify
from digest.errors import DigestError, VaultError
from digest.fetchers import VideoOptions
from digest.pipeline import DigestModel, DuplicateError, make_note

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RESTORED = "⏳ После перезапуска снова в очереди…"


@dataclass(frozen=True)
class Job:
    url: str
    hint: str
    chat_id: int
    status_msg_id: int
    force: bool = False  # «заново»: не проверять дубликаты
    id: int | None = None  # строка в link_jobs; None — задача не из БД


class NoteStore(Protocol):
    async def save(self, title: str, markdown: str, day: date) -> str: ...


async def _edit(bot, job: Job, text: str) -> None:
    """Редактируем статус-сообщение; если не вышло (удалено и т.п.) — только лог."""
    try:
        await bot.edit_message_text(text=text, chat_id=job.chat_id, message_id=job.status_msg_id)
    except Exception:
        log.exception("Не смог обновить статус для %s", job.url)


async def process_job(
    job: Job,
    *,
    bot,
    llm: DigestModel,
    http: httpx.AsyncClient,
    vault: NoteStore,
    today: Callable[[], date],
    db: Database,
    video: VideoOptions | None = None,
) -> None:
    start = time.monotonic()
    status = "failed"
    if job.id is not None:
        db.start_job(job.id)
    try:

        async def progress(text: str) -> None:
            await _edit(bot, job, text)

        day = today()
        note = await make_note(
            job.url,
            job.hint,
            llm=llm,
            http=http,
            today=day,
            video=video,
            known=None if job.force else db.find_digest,
            progress=progress,
        )
        d = note.digest
        source = classify(job.url).value
        try:
            name = await vault.save(d.title, note.markdown, day)
        except VaultError as err:
            if err.note_name:  # закоммичена локально — уйдёт со следующим push
                db.save_digest(note.keys, err.note_name, job.url, source)
            raise
        db.save_digest(note.keys, name, job.url, source)
        status = "done"
        await _edit(
            bot,
            job,
            f"{escape(d.emoji)} <b>{escape(d.title)}</b>\n\n{escape(d.tldr)}\n\n"
            f"📝 Заметка: «{escape(name)}»",
        )
    except asyncio.CancelledError:
        # Остановка бота посреди задачи — не падение: вернётся после старта (restore_jobs).
        status = "pending"
        raise
    except DuplicateError as err:
        status = "done"
        await _edit(bot, job, f"♻️ Уже есть: «{escape(err.note_name)}»")
    except DigestError as err:
        log.info("Конспект не получился для %s: %s", job.url, err)
        await _edit(bot, job, f"❌ Не получилось: {escape(str(err))}")
    except Exception:
        log.exception("Ошибка при обработке %s", job.url)
        await _edit(bot, job, "❌ Не получилось: внутренняя ошибка, подробности в логах.")
    finally:
        if job.id is not None:
            if status == "pending":
                db.release_job(job.id)
            else:
                db.finish_job(job.id, status)
        log.info("⏱ всего: %.1f с, %s (%s)", time.monotonic() - start, status, job.url)


async def restore_jobs(db: Database, queue: asyncio.Queue[Job], bot) -> int:
    """Недоделанные до перезапуска задачи — снова в очередь. Возвращает, сколько вернули."""
    restored = 0
    for row in db.pending_jobs():
        job = Job(
            url=row.url,
            hint=row.hint,
            chat_id=row.chat_id,
            status_msg_id=row.status_msg_id,
            force=row.force,
            id=row.id,
        )
        if row.attempts >= MAX_ATTEMPTS:
            db.finish_job(row.id, "failed")
            log.error("Задача %s (%s) падала %d раз — снята", row.id, row.url, row.attempts)
            await _edit(
                bot,
                job,
                f"❌ Не получилось: бот падал на этой ссылке {MAX_ATTEMPTS} раза, "
                "больше не пробую.",
            )
            continue
        queue.put_nowait(job)
        restored += 1
        await _edit(bot, job, RESTORED)
    return restored


async def run_worker(queue: asyncio.Queue[Job], **deps) -> None:
    while True:
        job = await queue.get()
        try:
            await process_job(job, **deps)
        except Exception:  # process_job сам всё ловит; это последняя страховка
            log.exception("Воркер: непредвиденная ошибка на %s", job.url)
        finally:
            queue.task_done()
