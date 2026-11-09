"""Очередь ссылок в памяти: задачи обрабатываются по одной, воркер никогда не падает.

После перезапуска бота очередь теряется — так решено для этапа 1.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Protocol

import httpx

from digest.errors import DigestError
from digest.pipeline import DigestModel, make_note

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    url: str
    hint: str
    chat_id: int
    status_msg_id: int


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
) -> None:
    try:
        await _edit(bot, job, "🧠 Смотрю видео…")
        day = today()
        d, md = await make_note(job.url, job.hint, llm=llm, http=http, today=day)
        name = await vault.save(d.title, md, day)
        await _edit(
            bot,
            job,
            f"{escape(d.emoji)} <b>{escape(d.title)}</b>\n\n{escape(d.tldr)}\n\n"
            f"📝 Заметка: «{escape(name)}»",
        )
    except DigestError as err:
        log.info("Конспект не получился для %s: %s", job.url, err)
        await _edit(bot, job, f"❌ Не получилось: {escape(str(err))}")
    except Exception:
        log.exception("Ошибка при обработке %s", job.url)
        await _edit(bot, job, "❌ Не получилось: внутренняя ошибка, подробности в логах.")


async def run_worker(queue: asyncio.Queue[Job], **deps) -> None:
    while True:
        job = await queue.get()
        try:
            await process_job(job, **deps)
        except Exception:  # process_job сам всё ловит; это последняя страховка
            log.exception("Воркер: непредвиденная ошибка на %s", job.url)
        finally:
            queue.task_done()
