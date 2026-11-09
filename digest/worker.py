"""Очередь ссылок: задачи обрабатываются по одной, воркер никогда не падает.

Задачи лежат в SQLite (link_jobs): после перезапуска restore_jobs возвращает недоделанные
в очередь. Задача, на которой бот падал MAX_ATTEMPTS раз, больше не повторяется.

Gemini перегружен (обе модели) → задача откладывается (5/15/30 мин, дневная квота — до сброса)
и возвращается в очередь по таймеру Postponer, а после перезапуска — по run_after из базы.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from html import escape
from typing import Protocol

import httpx

from config import QUOTA_RESET_TZ
from database import Database
from digest.classifier import classify
from digest.errors import DigestError, GeminiBusy, VaultError
from digest.fetchers import VideoOptions
from digest.pipeline import DigestModel, DuplicateError, make_note

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RESTORED = "⏳ После перезапуска снова в очереди…"
BUSY_DELAYS_MIN = (5, 15, 30)  # откладывания при перегрузке Gemini
MAX_QUOTA_WAITS = 1  # ждать сброса дневной квоты — не больше раза на задачу
QUOTA_RESET_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class Job:
    url: str
    hint: str
    chat_id: int
    status_msg_id: int
    force: bool = False  # «заново»: не проверять дубликаты
    id: int | None = None  # строка в link_jobs; None — задача не из БД
    defers: int = 0  # сколько раз откладывали из-за перегрузки Gemini
    quota_waits: int = 0  # сколько раз ждали сброса дневной квоты


class Later(Protocol):
    def later(self, job: Job, at: datetime) -> None: ...


class Postponer:
    """Возвращает задачу в очередь в момент `at`, не занимая воркер ожиданием."""

    def __init__(self, queue: asyncio.Queue[Job]):
        self.queue = queue
        self._tasks: set[asyncio.Task] = set()

    def later(self, job: Job, at: datetime) -> None:
        delay = max(0.0, (at - datetime.now(UTC)).total_seconds())
        task = asyncio.get_running_loop().create_task(self._put(job, delay))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _put(self, job: Job, delay: float) -> None:
        await asyncio.sleep(delay)
        log.info("Отложенная задача %s снова в очереди (%s)", job.id, job.url)
        self.queue.put_nowait(job)

    def close(self) -> None:
        """Отменить таймеры; задачи остаются в базе pending и вернутся после старта."""
        for task in list(self._tasks):
            task.cancel()


def _hhmm(at: datetime, now: datetime) -> str:
    return at.astimezone(now.tzinfo).strftime("%H:%M")


def next_quota_reset(now: datetime) -> datetime:
    """Полночь по тихоокеанскому времени (сброс дневной квоты Gemini) + 5 мин, в поясе now."""
    local = now.astimezone(QUOTA_RESET_TZ)
    today_reset = (
        datetime.combine(local.date(), dtime(0), tzinfo=QUOTA_RESET_TZ) + QUOTA_RESET_GRACE
    )
    # Ошибка в первые минуты после полуночи — сброс ещё «доезжает», ждём сегодняшние 00:05.
    reset = today_reset if local < today_reset else today_reset + timedelta(days=1)
    return reset.astimezone(now.tzinfo)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _postpone(
    job: Job, err: GeminiBusy, *, db: Database, clock: Callable[[], datetime], postponer
) -> tuple[bool, str]:
    """Отложить задачу: (True, текст статуса) или (False, текст ошибки), если лимит исчерпан.

    Без await: база и таймер обновляются до правки сообщения, поэтому остановка бота во время
    этой правки не теряет задачу.
    """
    now = clock()
    if err.daily:
        if job.quota_waits >= MAX_QUOTA_WAITS:
            return (
                False,
                "❌ Не получилось: лимит Gemini на сегодня исчерпан — пришли ссылку позже.",
            )
        at = next_quota_reset(now)
        again = replace(job, quota_waits=job.quota_waits + 1)
        text = f"⏳ Лимит Gemini на сегодня исчерпан — попробую снова в {_hhmm(at, now)}"
    else:
        if job.defers >= len(BUSY_DELAYS_MIN):
            return False, "❌ Не получилось: Gemini перегружен уже ~50 минут — пришли ссылку позже."
        at = now + timedelta(minutes=BUSY_DELAYS_MIN[job.defers])
        again = replace(job, defers=job.defers + 1)
        text = (
            f"⏳ Gemini перегружен, попробую снова в {_hhmm(at, now)} "
            f"(повтор {again.defers} из {len(BUSY_DELAYS_MIN)})"
        )
    if job.id is not None:
        db.postpone_job(job.id, at, daily=err.daily)
    postponer.later(again, at)
    log.info("Gemini перегружен: задача %s отложена до %s (%s)", job.id, at, job.url)
    return True, text


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
    clock: Callable[[], datetime] = _utc_now,
    postponer: Later | None = None,
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
    except GeminiBusy as err:
        if postponer is None:  # некуда откладывать (нет очереди) — как обычная ошибка
            await _edit(bot, job, f"❌ Не получилось: {escape(str(err))}")
        else:
            postponed, text = _postpone(job, err, db=db, clock=clock, postponer=postponer)
            if postponed:
                status = "postponed"  # до правки: отмена во время неё не сделает задачу failed
            await _edit(bot, job, text)
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
        if job.id is not None and status != "postponed":
            if status == "pending":
                db.release_job(job.id)
            else:
                db.finish_job(job.id, status)
        log.info("⏱ всего: %.1f с, %s (%s)", time.monotonic() - start, status, job.url)


async def restore_jobs(
    db: Database,
    queue: asyncio.Queue[Job],
    bot,
    *,
    postponer: Later | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Недоделанные до перезапуска задачи — снова в очередь (отложенные — к своему времени).

    В каждый чат с задачами уходит новое сообщение-сводка: правка статуса уведомления не даёт,
    а первая задача всё равно сразу перезапишет «⏳».
    """
    now = clock()
    restored: Counter[int] = Counter()
    waiting: dict[int, list[datetime]] = {}
    for row in db.pending_jobs():
        job = Job(
            url=row.url,
            hint=row.hint,
            chat_id=row.chat_id,
            status_msg_id=row.status_msg_id,
            force=row.force,
            id=row.id,
            defers=row.defers,
            quota_waits=row.quota_waits,
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
        restored[row.chat_id] += 1
        if postponer is not None and row.run_after is not None and row.run_after > now:
            postponer.later(job, row.run_after)  # статус «попробую снова в HH:MM» не трогаем
            waiting.setdefault(row.chat_id, []).append(row.run_after)
            continue
        queue.put_nowait(job)
        await _edit(bot, job, RESTORED)

    for chat_id, count in restored.items():
        text = f"🔄 Бот перезапущен — вернул в очередь ссылок: {count}"
        if times := waiting.get(chat_id):
            text += f", ждут Gemini: {len(times)} (ближайшая в {_hhmm(min(times), now)})"
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("Не смог отправить сводку после перезапуска в чат %s", chat_id)
    return sum(restored.values())


async def run_worker(queue: asyncio.Queue[Job], **deps) -> None:
    while True:
        job = await queue.get()
        try:
            await process_job(job, **deps)
        except Exception:  # process_job сам всё ловит; это последняя страховка
            log.exception("Воркер: непредвиденная ошибка на %s", job.url)
        finally:
            queue.task_done()
