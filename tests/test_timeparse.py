from datetime import datetime, timedelta

import pytest

from services.timeparse import Repeat, format_dt, next_occurrence, parse_reminder_time, shift
from tests.conftest import TZ

# --- T1: абсолютные форматы, всегда aware -----------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18:30", datetime(2026, 11, 8, 18, 30, tzinfo=TZ)),
        ("18:30:15", datetime(2026, 11, 8, 18, 30, 15, tzinfo=TZ)),
        ("9:05", datetime(2026, 11, 9, 9, 5, tzinfo=TZ)),
        ("2026-12-31 23:59", datetime(2026, 12, 31, 23, 59, tzinfo=TZ)),
        ("2026-12-31 23:59:30", datetime(2026, 12, 31, 23, 59, 30, tzinfo=TZ)),
        ("  2026-11-08 13:00  ", datetime(2026, 11, 8, 13, 0, tzinfo=TZ)),
    ],
)
def test_absolute_formats(text, expected, now):
    result = parse_reminder_time(text, now)
    assert result == expected
    assert result.tzinfo is not None


# --- T2: прошлое ------------------------------------------------------------------


def test_time_earlier_today_moves_to_tomorrow(now):
    assert parse_reminder_time("11:00", now) == datetime(2026, 11, 9, 11, 0, tzinfo=TZ)


def test_time_equal_to_now_moves_to_tomorrow(now):
    assert parse_reminder_time("12:00", now) == datetime(2026, 11, 9, 12, 0, tzinfo=TZ)


def test_past_date_rejected(now):
    with pytest.raises(ValueError, match="прошл"):
        parse_reminder_time("2026-11-08 11:59", now)


def test_today_in_past_rejected(now):
    with pytest.raises(ValueError, match="прошл"):
        parse_reminder_time("сегодня 10:00", now)


# --- T3: неверный формат ----------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["", "завтра", "25:00", "2026-13-01 10:00", "через", "через -5 мин", "через 0 мин"]
)
def test_invalid_input_lists_formats(text, now):
    with pytest.raises(ValueError) as err:
        parse_reminder_time(text, now)
    assert "HH:MM" in str(err.value)
    assert "через" in str(err.value)


# --- T5: относительное время -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("через 20 мин", timedelta(minutes=20)),
        ("через 20 минут", timedelta(minutes=20)),
        ("Через 1 минуту", timedelta(minutes=1)),
        ("через 2 ч", timedelta(hours=2)),
        ("через 3 часа", timedelta(hours=3)),
        ("через 1 час", timedelta(hours=1)),
        ("через 1 д", timedelta(days=1)),
        ("через 2 дня", timedelta(days=2)),
        ("через 1 ч 30 мин", timedelta(hours=1, minutes=30)),
        ("20m", timedelta(minutes=20)),
        ("2h", timedelta(hours=2)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("1d", timedelta(days=1)),
    ],
)
def test_relative(text, delta, now):
    assert parse_reminder_time(text, now) == now + delta


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("завтра 9:00", datetime(2026, 11, 9, 9, 0, tzinfo=TZ)),
        ("завтра в 9:00", datetime(2026, 11, 9, 9, 0, tzinfo=TZ)),
        ("послезавтра 18:30", datetime(2026, 11, 10, 18, 30, tzinfo=TZ)),
        ("сегодня 21:00", datetime(2026, 11, 8, 21, 0, tzinfo=TZ)),
        ("Сегодня в 21:00", datetime(2026, 11, 8, 21, 0, tzinfo=TZ)),
    ],
)
def test_day_words(text, expected, now):
    assert parse_reminder_time(text, now) == expected


def test_relative_across_dst_is_real_elapsed_time():
    """«через 2 ч» в ночь перехода на зимнее время — это 2 реальных часа."""
    from datetime import UTC

    before = datetime(2026, 10, 25, 2, 30, tzinfo=TZ)  # 02:30 EEST, в 04:00 часы → 03:00
    result = parse_reminder_time("через 2 ч", before)
    assert result.astimezone(UTC) - before.astimezone(UTC) == timedelta(hours=2)
    local = result.astimezone(TZ)
    assert (local.hour, local.minute) == (3, 30)  # 02:30 + 2 реальных часа = 03:30 EET


def test_result_has_no_microseconds(now):
    assert parse_reminder_time("через 5 мин", now.replace(microsecond=123)).microsecond == 0


# --- R2: следующее вхождение -----------------------------------------------------


def test_next_daily(now):
    base = datetime(2026, 11, 8, 9, 0, tzinfo=TZ)
    assert next_occurrence(base, Repeat.DAILY, now) == datetime(2026, 11, 9, 9, 0, tzinfo=TZ)


def test_next_weekly(now):
    base = datetime(2026, 11, 8, 9, 0, tzinfo=TZ)
    assert next_occurrence(base, Repeat.WEEKLY, now) == datetime(2026, 11, 15, 9, 0, tzinfo=TZ)


def test_next_skips_many_missed_days(now):
    base = datetime(2026, 11, 1, 9, 0, tzinfo=TZ)
    assert next_occurrence(base, Repeat.DAILY, now) == datetime(2026, 11, 9, 9, 0, tzinfo=TZ)


def test_next_keeps_local_wall_time_across_dst():
    """R2: 9:00 остаётся 9:00 местного времени после перехода на зимнее время."""
    base = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)  # EEST, UTC+3
    now = datetime(2026, 10, 24, 9, 0, 1, tzinfo=TZ)
    nxt = next_occurrence(base, Repeat.DAILY, now)
    local = nxt.astimezone(TZ)
    assert (local.date().isoformat(), local.hour, local.minute) == ("2026-10-25", 9, 0)
    assert local.utcoffset() == timedelta(hours=2)


def test_next_from_utc_stored_value_keeps_local_time():
    """Из БД приходит UTC — всё равно считаем по местному времени."""
    from datetime import UTC

    base_utc = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)  # = 09:00 EEST
    nxt = next_occurrence(base_utc, Repeat.DAILY, base_utc, tz=TZ).astimezone(TZ)
    assert (nxt.day, nxt.hour) == (25, 9)


def test_shift_back(now):
    base = datetime(2026, 11, 9, 9, 0, tzinfo=TZ)
    assert shift(base, Repeat.DAILY, -1) == datetime(2026, 11, 8, 9, 0, tzinfo=TZ)
    assert shift(base, Repeat.WEEKLY, -1) == datetime(2026, 11, 2, 9, 0, tzinfo=TZ)


def test_format_dt_local():
    from datetime import UTC

    assert format_dt(datetime(2026, 11, 8, 10, 0, tzinfo=UTC), TZ) == "08.11.2026 12:00"
