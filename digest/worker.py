"""Очередь ссылок: задачи обрабатываются по одной, воркер никогда не падает.

Задачи лежат в SQLite (link_jobs): после перезапуска restore_jobs возвращает недоделанные
в очередь. Задача, на которой бот падал MAX_ATTEMPTS раз, больше не повторяется.

Gemini перегружен (обе модели) → задача откладывается (5/15/30 мин, дневная квота — до сброса)
и возвращается в очередь по таймеру Postponer, а после перезапуска — по run_after из базы.

Под готовым конспектом (и под «♻️ Уже есть») — кнопки 🔁 Подробнее / ✅ Прочитано / 🗑 Удалить.
Задача с note_id («Подробнее») перезаписывает ту же заметку вместо новой.
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
from digest.classifier import canonical_youtube, classify, normalize, youtube_id
from digest.errors import DigestError, GeminiBusy, VaultError, VaultQueued
from digest.fetchers import VideoOptions
from digest.pipeline import (
    WHISPER_MAX_SECONDS,
    DigestModel,
    DuplicateError,
    Transcribes,
    make_note,
)
from digest.vault_base import VaultBackend
from handlers import keyboards as kb

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
    note_id: int | None = None  # «Подробнее»: перезаписать эту заметку (notes.id)


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


async def _edit(bot, job: Job, text: str, markup=None) -> None:
    """Редактируем статус-сообщение; если не вышло (удалено и т.п.) — только лог."""
    try:
        await bot.edit_message_text(
            text=text, chat_id=job.chat_id, message_id=job.status_msg_id, reply_markup=markup
        )
    except Exception:
        log.exception("Не смог обновить статус для %s", job.url)


def _summary(d, name: str) -> str:
    return (
        f"{escape(d.emoji)} <b>{escape(d.title)}</b>\n\n{escape(d.tldr)}\n\n"
        f"📝 Заметка: «{escape(name)}»"
    )


async def process_job(
    job: Job,
    *,
    bot,
    llm: DigestModel,
    http: httpx.AsyncClient,
    vault: VaultBackend,
    today: Callable[[], date],
    db: Database,
    video: VideoOptions | None = None,
    clock: Callable[[], datetime] = _utc_now,
    postponer: Later | None = None,
    transcriber: Transcribes | None = None,
    whisper_max_seconds: int = WHISPER_MAX_SECONDS,
    outbox=None,
    detail_llm: DigestModel | None = None,
) -> None:
    """outbox (режим api) — к ждущей отправки операции привязывается статус-сообщение;
    detail_llm — модель для «Подробнее» (GEMINI_DETAIL_MODEL), иначе основная."""
    start = time.monotonic()
    status = "failed"
    if job.id is not None:
        db.start_job(job.id)
    try:

        async def progress(text: str) -> None:
            await _edit(bot, job, text)

        target = None
        if job.note_id is not None:
            target = db.get_note(job.note_id)
            if target is None:  # удалили, пока задача ждала в очереди — квоту не тратим
                raise DigestError("заметка уже удалена — подробный конспект не делаю.")

        known = None if job.force else db.find_digest
        if not job.force and target is None:
            remote = await _remote_names(vault, job.url)
            if remote:
                raise DuplicateError(remote[0], [normalize(job.url)])
            if remote is not None:
                known = None  # сайт ответил «нет» — правда у него, а не у локальной базы

        day = today()
        model = detail_llm if target is not None and detail_llm is not None else llm
        note = await make_note(
            job.url,
            job.hint,
            llm=model,
            http=http,
            today=day,
            video=video,
            known=known,
            progress=progress,
            transcriber=transcriber,
            whisper_max_seconds=whisper_max_seconds,
        )
        d = note.digest
        if target is not None:
            warning = ""
            queued = None
            try:
                name = await vault.replace(
                    target.note_name,
                    target.url,
                    note.markdown,
                    skip=db.other_note_names(target.id),
                )
            except VaultQueued as err:  # сайт недоступен — подробный текст в очереди
                name = err.note_name or target.note_name
                warning = f"\n\n{escape(str(err))}"
                queued = (err.op_id, "🔁 Подробный конспект готов\n\n" + _summary(d, name))
            except VaultError as err:
                if not err.note_name:  # не закоммичено — как обычная ошибка
                    raise
                name = err.note_name  # закоммичено локально, уйдёт со следующим push
                warning = f"\n\n⚠️ {escape(str(err))}"
            if name != target.note_name:
                db.rename_note(target.id, name)
            db.mark_read(target.id, False)  # новый текст — снова status: unread
            status = "done"
            await _edit(
                bot,
                job,
                "🔁 Подробный конспект готов\n\n" + _summary(d, name) + warning,
                kb.note_actions(target.id, detailed=True, read=False),
            )
            if queued is not None:
                await _attach(bot, job, outbox, queued[0], queued[1], target.id)
            return

        source = classify(job.url).value
        try:
            name = await vault.save(d.title, note.markdown, day)
        except VaultQueued as err:  # сайт недоступен: заметка ждёт в outbox, кнопки уже работают
            name = err.note_name or d.title
            note_id = db.add_note(name, note.url or job.url, source, job.chat_id)
            db.save_digest(note.keys, name, job.url, source, note_id=note_id)
            status = "done"
            text = _summary(d, name)
            await _edit(bot, job, f"{text}\n\n{escape(str(err))}", kb.note_actions(note_id))
            await _attach(bot, job, outbox, err.op_id, text, note_id)
            return
        except VaultError as err:
            if err.note_name:  # закоммичена локально — уйдёт со следующим push
                note_id = db.add_note(err.note_name, note.url or job.url, source, job.chat_id)
                db.save_digest(note.keys, err.note_name, job.url, source, note_id=note_id)
            raise
        note_id = db.add_note(name, note.url or job.url, source, job.chat_id)
        db.save_digest(note.keys, name, job.url, source, note_id=note_id)
        status = "done"
        await _edit(bot, job, _summary(d, name), kb.note_actions(note_id))
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
        markup = None
        vid = youtube_id(job.url)
        note_url = canonical_youtube(vid) if vid else job.url  # как в самой заметке
        note_id = db.note_id_for(err.keys, job.chat_id, url=note_url)
        if note_id is None:  # нашёл сайт (lookup), а в своей базе строки нет — заводим
            source = classify(job.url).value
            note_id = db.add_note(err.note_name, note_url, source, job.chat_id)
            db.save_digest(err.keys, err.note_name, job.url, source, note_id=note_id)
        if (row := db.get_note(note_id)) is not None:
            markup = kb.note_actions(row.id, detailed=row.detailed, read=row.read)
        await _edit(bot, job, f"♻️ Уже есть: «{escape(err.note_name)}»", markup)
    except DigestError as err:
        log.info("Конспект не получился для %s: %s", job.url, err)
        await _edit(bot, job, f"❌ Не получилось: {escape(str(err))}")
    except Exception:
        log.exception("Ошибка при обработке %s", job.url)
        await _edit(bot, job, "❌ Не получилось: внутренняя ошибка, подробности в логах.")
    finally:
        if job.note_id is not None and status == "failed":
            db.mark_detailed(job.note_id, False)  # не вышло — кнопку «Подробнее» можно снова
        if job.id is not None and status != "postponed":
            if status == "pending":
                db.release_job(job.id)
            else:
                db.finish_job(job.id, status)
        log.info("⏱ всего: %.1f с, %s (%s)", time.monotonic() - start, status, job.url)


async def _remote_names(vault: VaultBackend, url: str) -> list[str] | None:
    """Дубликаты по хранилищу (api: lookup); None — не знает, проверяем по своей базе."""
    try:
        return await vault.find_by_url(url)
    except VaultError as err:
        log.warning("Дубликаты: хранилище не ответило (%s) — проверяю по локальной базе", err)
        return None


async def _attach(bot, job: Job, outbox, op_id: int, text: str, note_id: int) -> None:
    """Сообщение обновится, когда операция дойдёт до сайта. Дошла, пока мы правили «📮», —
    обновляем сразу (фоновая отправка не знала, какое сообщение трогать)."""
    if outbox is None:
        return
    outbox.attach(op_id, chat_id=job.chat_id, msg_id=job.status_msg_id, text=text, note_id=note_id)
    op = outbox.db.get_op(op_id)
    if op is not None and op.status == "done":
        await outbox.notify_done(op_id)


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
            note_id=row.note_id,
        )
        if row.attempts >= MAX_ATTEMPTS:
            db.finish_job(row.id, "failed")
            if row.note_id is not None:
                db.mark_detailed(row.note_id, False)
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
