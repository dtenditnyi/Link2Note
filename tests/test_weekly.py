"""Этап 3: воскресный дайджест непрочитанного (D1–D4)."""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from digest.vault import UnreadNote
from digest.weekly import schedule_weekly, send_weekly, unread_text
from tests.conftest import OWNER, TZ


def _notes(n: int) -> list[UnreadNote]:
    return [UnreadNote(f"n{i}", f"⌨️ Заметка <{i}>", date(2026, 11, i + 1), i + 1) for i in range(n)]


def test_d2_text_top5_oldest_and_rest():
    text = unread_text(_notes(7))
    assert text == (
        "📚 <b>Непрочитанных конспектов: 7</b>\n\n"
        "1. ⌨️ Заметка &lt;0&gt; — 1 мин · 01.11\n"
        "2. ⌨️ Заметка &lt;1&gt; — 2 мин · 02.11\n"
        "3. ⌨️ Заметка &lt;2&gt; — 3 мин · 03.11\n"
        "4. ⌨️ Заметка &lt;3&gt; — 4 мин · 04.11\n"
        "5. ⌨️ Заметка &lt;4&gt; — 5 мин · 05.11\n\n"
        "…и ещё 2"
    )


def test_d2_text_without_date():
    text = unread_text([UnreadNote("n", "Без даты", None, 2)])
    assert text.endswith("1. Без даты — 2 мин")


def test_d2_nothing_unread():
    assert unread_text([]) is None


async def test_d2_send_weekly():
    bot = MagicMock(send_message=AsyncMock())
    vault = MagicMock(unread=AsyncMock(return_value=_notes(1)))
    await send_weekly(bot=bot, vault=vault, chat_id=OWNER)
    chat, text = bot.send_message.call_args.args
    assert chat == OWNER and "Непрочитанных конспектов: 1" in text


async def test_d2_send_weekly_nothing_sends_nothing():
    bot = MagicMock(send_message=AsyncMock())
    vault = MagicMock(unread=AsyncMock(return_value=[]))
    await send_weekly(bot=bot, vault=vault, chat_id=OWNER)
    bot.send_message.assert_not_awaited()


async def test_d4_send_weekly_errors_are_logged_not_raised():
    bot = MagicMock(send_message=AsyncMock(side_effect=RuntimeError("telegram down")))
    vault = MagicMock(unread=AsyncMock(return_value=_notes(1)))
    await send_weekly(bot=bot, vault=vault, chat_id=OWNER)  # не падает
    vault = MagicMock(unread=AsyncMock(side_effect=RuntimeError("git")))
    await send_weekly(bot=bot, vault=vault, chat_id=OWNER)


def test_d1_schedule_sunday_18():
    aps = AsyncIOScheduler(timezone=TZ)
    bot, vault = MagicMock(), MagicMock()
    schedule_weekly(aps, bot=bot, vault=vault, chat_id=OWNER)
    job = aps.get_job("weekly-digest")
    assert job.func is send_weekly
    assert job.kwargs == {"bot": bot, "vault": vault, "chat_id": OWNER}
    assert job.coalesce is True and job.misfire_grace_time == 3600
    sunday_noon = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)
    assert job.trigger.get_next_fire_time(None, sunday_noon) == datetime(
        2026, 11, 8, 18, 0, tzinfo=TZ
    )
    sunday_evening = datetime(2026, 11, 8, 18, 30, tzinfo=TZ)
    assert job.trigger.get_next_fire_time(None, sunday_evening) == datetime(
        2026, 11, 15, 18, 0, tzinfo=TZ
    )
