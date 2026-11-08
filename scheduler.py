"""Планировщик напоминаний: APScheduler (в памяти) + SQLite как источник правды."""

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from html import escape

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import Database, Reminder
from handlers import keyboards as kb
from services.timeparse import REPEAT_LABELS, Repeat, format_dt, next_occurrence, shift

log = logging.getLogger(__name__)


def _job_id(reminder_id: int) -> str:
    return f"reminder-{reminder_id}"


class ReminderScheduler:
    def __init__(
        self,
        bot,
        db: Database,
        tz: tzinfo,
        *,
        scheduler=None,
        clock: Callable[[], datetime] | None = None,
        retry_delay: timedelta = timedelta(minutes=1),
        max_attempts: int = 3,
        missed_window: timedelta = timedelta(hours=24),
    ):
        self.bot = bot
        self.db = db
        self.tz = tz
        self.scheduler = scheduler or AsyncIOScheduler(timezone=tz)
        self.clock = clock or (lambda: datetime.now(tz))
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self.missed_window = missed_window

    async def start(self) -> None:
        self.scheduler.start()
        await self.restore()

    # --- задачи -----------------------------------------------------------------

    def schedule(self, reminder: Reminder) -> None:
        self._add_job(reminder.id, reminder.remind_at, attempt=1)

    def unschedule(self, reminder_id: int) -> None:
        try:
            self.scheduler.remove_job(_job_id(reminder_id))
        except JobLookupError:
            pass

    def _add_job(self, reminder_id: int, run_date: datetime, attempt: int, missed_at=None) -> None:
        kwargs = {"reminder_id": reminder_id, "attempt": attempt}
        if missed_at is not None:
            kwargs["missed_at"] = missed_at
        self.scheduler.add_job(
            self.fire,
            "date",
            id=_job_id(reminder_id),
            run_date=run_date,
            replace_existing=True,
            # По умолчанию 1 с: опоздавшая задача молча пропускалась бы.
            misfire_grace_time=None,
            kwargs=kwargs,
        )

    # --- срабатывание -----------------------------------------------------------

    def _text(self, r: Reminder, missed_at: datetime | None) -> str:
        task = escape(r.task)
        if missed_at is not None:
            text = (
                f"⏰ <b>Пропущено</b>, должно было сработать в {format_dt(missed_at, self.tz)}\n"
                f"{task}"
            )
        else:
            text = f"🔔 <b>Напоминание:</b> {task}"
        if r.repeat is not Repeat.NONE:
            text += f"\n🔁 {REPEAT_LABELS[r.repeat]}"
        return text

    async def fire(
        self, reminder_id: int, attempt: int = 1, missed_at: datetime | None = None
    ) -> None:
        r = self.db.get_by_id(reminder_id)
        if r is None or r.status != "pending":
            return
        try:
            await self.bot.send_message(
                r.chat_id, self._text(r, missed_at), reply_markup=kb.snooze(r.id)
            )
        except Exception:
            log.exception("Failed to send reminder %s (attempt %s)", r.id, attempt)
            if attempt < self.max_attempts:
                self._add_job(r.id, self.clock() + self.retry_delay, attempt + 1, missed_at)
            elif r.repeat is Repeat.NONE:
                if self.db.finish(r.id, "failed", r.remind_at):
                    log.error("Reminder %s marked failed after %s attempts", r.id, attempt)
            else:
                self._advance(r)
            return
        if r.repeat is Repeat.NONE:
            # Если во время отправки время изменили/отложили — правка пользователя важнее.
            self.db.finish(r.id, "sent", r.remind_at)
        else:
            self._advance(r)

    def _advance(self, r: Reminder) -> None:
        """Повторяющееся: следующее вхождение по местному времени."""
        nxt = next_occurrence(r.remind_at, r.repeat, self.clock(), self.tz)
        if self.db.advance(r.id, r.remind_at, nxt):
            self._add_job(r.id, nxt, attempt=1)
        # иначе напоминание изменили/удалили во время отправки — новая задача уже стоит

    # --- старт: восстановление и пропущенные ------------------------------------

    async def restore(self) -> None:
        now = self.clock()
        old: dict[int, list[tuple[Reminder, datetime]]] = defaultdict(list)
        for r in self.db.all_pending():
            if r.remind_at > now:
                self.schedule(r)
                continue
            if r.repeat is Repeat.NONE:
                last, nxt = r.remind_at, None
            else:
                nxt = next_occurrence(r.remind_at, r.repeat, now, self.tz)
                last = shift(nxt, r.repeat, -1, self.tz)
            if now - last <= self.missed_window:
                await self.fire(r.id, missed_at=last)
                continue
            old[r.chat_id].append((r, last))
            if nxt is None:
                self.db.set_status(r.id, "missed")
            else:
                self.db.update_time(r.id, r.chat_id, nxt)
                self._add_job(r.id, nxt, attempt=1)

        for chat_id, items in old.items():
            lines = [f"⏰ Пока бот был выключен, пропущено напоминаний: {len(items)}"]
            lines += [f"• {format_dt(last, self.tz)} — {escape(r.task)}" for r, last in items]
            try:
                await self.bot.send_message(chat_id, "\n".join(lines))
            except Exception:
                log.exception("Failed to send missed summary to %s", chat_id)
