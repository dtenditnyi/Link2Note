from datetime import datetime, timedelta

import pytest

from scheduler import ReminderScheduler
from services.timeparse import Repeat
from tests.conftest import OWNER, TZ, sent_texts


@pytest.fixture
def sched(bot, db, aps, now):
    return ReminderScheduler(bot, db, TZ, scheduler=aps, clock=lambda: now)


def test_schedule_adds_date_job_without_grace(sched, db, aps, now):
    """S1 + S4."""
    rid = db.add(OWNER, "x", now + timedelta(hours=1))
    sched.schedule(db.get(rid, OWNER))
    job = aps.jobs[f"reminder-{rid}"]
    assert job["run_date"] == now + timedelta(hours=1)
    assert job["misfire_grace_time"] is None


def test_unschedule_missing_job_is_noop(sched):
    sched.unschedule(12345)


async def test_fire_sends_escaped_and_marks_sent(sched, db, bot, now):
    """S2 + A5."""
    rid = db.add(OWNER, "купить <молоко> & хлеб", now)
    await sched.fire(rid)
    text = sent_texts(bot.send_message)[0]
    assert "купить &lt;молоко&gt; &amp; хлеб" in text
    assert db.get(rid, OWNER).status == "sent"
    assert bot.send_message.call_args.kwargs["reply_markup"] is not None  # S7: кнопки


async def test_fire_skips_non_pending(sched, db, bot, now):
    rid = db.add(OWNER, "x", now)
    db.set_status(rid, "done")
    await sched.fire(rid)
    bot.send_message.assert_not_called()


async def test_fire_deleted_reminder_is_noop(sched, bot):
    await sched.fire(777)
    bot.send_message.assert_not_called()


async def test_fire_failure_keeps_pending_and_retries(sched, db, bot, aps, now):
    """S2: ошибка отправки → pending + повтор через минуту."""
    rid = db.add(OWNER, "x", now)
    bot.send_message.side_effect = RuntimeError("network")
    await sched.fire(rid)
    assert db.get(rid, OWNER).status == "pending"
    job = aps.jobs[f"reminder-{rid}"]
    assert job["run_date"] == now + timedelta(minutes=1)
    assert job["kwargs"]["attempt"] == 2


async def test_fire_gives_up_after_three_attempts(sched, db, bot, aps, now):
    rid = db.add(OWNER, "x", now)
    bot.send_message.side_effect = RuntimeError("network")
    await sched.fire(rid, attempt=3)
    assert db.get(rid, OWNER).status == "failed"
    assert f"reminder-{rid}" not in aps.jobs


async def test_fire_daily_reschedules_next_day(sched, db, aps, now):
    """R2."""
    rid = db.add(OWNER, "зарядка", now, Repeat.DAILY)
    await sched.fire(rid)
    r = db.get(rid, OWNER)
    assert r.status == "pending"
    assert r.remind_at == now + timedelta(days=1)
    assert aps.jobs[f"reminder-{rid}"]["run_date"] == now + timedelta(days=1)


async def test_restore_schedules_future(sched, db, aps, bot, now):
    """S3: будущие перепланированы."""
    rid = db.add(OWNER, "x", now + timedelta(hours=2))
    await sched.restore()
    assert f"reminder-{rid}" in aps.jobs
    bot.send_message.assert_not_called()


async def test_restore_sends_recent_missed_with_mark(sched, db, bot, now):
    """S3/Q4: опоздание < 24 ч → отправить с пометкой."""
    rid = db.add(OWNER, "x", now - timedelta(hours=3))
    await sched.restore()
    text = sent_texts(bot.send_message)[0]
    assert "Пропущено" in text
    assert "08.11.2026 09:00" in text
    assert db.get(rid, OWNER).status == "sent"


async def test_restore_old_missed_summary(sched, db, bot, now):
    """Q4: старше 24 ч → missed + одна сводка."""
    a = db.add(OWNER, "старое А", now - timedelta(days=2))
    b = db.add(OWNER, "старое Б", now - timedelta(days=3))
    await sched.restore()
    assert db.get(a, OWNER).status == "missed"
    assert db.get(b, OWNER).status == "missed"
    texts = sent_texts(bot.send_message)
    assert len(texts) == 1
    assert "2" in texts[0] and "старое А" in texts[0] and "старое Б" in texts[0]


async def test_restore_daily_after_long_downtime_single_message(sched, db, bot, aps, now):
    """R3: бот лежал 5 дней — одно сообщение о пропуске, перенос на будущее."""
    base = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)
    rid = db.add(OWNER, "зарядка", base, Repeat.DAILY)
    await sched.restore()
    assert bot.send_message.call_count == 1
    assert "Пропущено" in sent_texts(bot.send_message)[0]
    r = db.get(rid, OWNER)
    assert r.status == "pending"
    assert r.remind_at == datetime(2026, 11, 9, 9, 0, tzinfo=TZ)
    assert f"reminder-{rid}" in aps.jobs


async def test_restore_weekly_last_occurrence_too_old_goes_to_summary(sched, db, bot, now):
    """Последнее вхождение недельного > 24 ч назад — в сводку, серия продолжается."""
    base = datetime(2026, 11, 4, 9, 0, tzinfo=TZ)  # среда; now — воскресенье
    rid = db.add(OWNER, "уборка", base, Repeat.WEEKLY)
    await sched.restore()
    texts = sent_texts(bot.send_message)
    assert len(texts) == 1 and "уборка" in texts[0]
    r = db.get(rid, OWNER)
    assert r.status == "pending"
    assert r.remind_at == datetime(2026, 11, 11, 9, 0, tzinfo=TZ)


async def test_start_starts_scheduler_and_restores(sched, aps, db, now):
    rid = db.add(OWNER, "x", now + timedelta(hours=1))
    await sched.start()
    assert aps.running
    assert f"reminder-{rid}" in aps.jobs


async def test_fire_does_not_overwrite_edit_made_during_send(sched, db, bot, aps, now):
    """Гонка: пока шла отправка, пользователь отложил/изменил время — правка побеждает."""
    rid = db.add(OWNER, "x", now)
    later = now + timedelta(hours=2)

    async def send_and_edit(*args, **kwargs):
        db.update_time(rid, OWNER, later)
        sched.schedule(db.get(rid, OWNER))

    bot.send_message.side_effect = send_and_edit
    await sched.fire(rid)
    r = db.get(rid, OWNER)
    assert (r.status, r.remind_at) == ("pending", later)
    assert aps.jobs[f"reminder-{rid}"]["run_date"] == later


async def test_fire_repeating_does_not_overwrite_edit(sched, db, bot, aps, now):
    rid = db.add(OWNER, "x", now, Repeat.DAILY)
    edited = now + timedelta(hours=5)

    async def send_and_edit(*args, **kwargs):
        db.update_time(rid, OWNER, edited)
        sched.schedule(db.get(rid, OWNER))

    bot.send_message.side_effect = send_and_edit
    await sched.fire(rid)
    assert db.get(rid, OWNER).remind_at == edited
    assert aps.jobs[f"reminder-{rid}"]["run_date"] == edited
