"""Время из текста напоминания (спецификация docs/specs/text-time-extraction.md, E1–E10)."""

from datetime import datetime, timedelta

import pytest

from services.text_reminder import clean_task, has_trigger, parse_text_reminder
from services.timeparse import extract_time
from tests.conftest import TZ


@pytest.mark.parametrize(
    ("text", "delta", "task"),
    [
        # E1, E2: время в начале и в конце
        ("через 2 минуты напомни про лимиты клода", timedelta(minutes=2), "лимиты клода"),
        ("напомни про лимиты клода через 2 минуты", timedelta(minutes=2), "лимиты клода"),
        ("Через 2 минуты напомни про лимиты клода", timedelta(minutes=2), "лимиты клода"),
        # E5: запятые вокруг времени
        (
            "напомни, через 1 ч 30 мин, выключить духовку",
            timedelta(hours=1, minutes=30),
            "выключить духовку",
        ),
        # E6: формы без числа
        ("через час напомни размяться", timedelta(hours=1), "размяться"),
        ("через полчаса напомни про чай", timedelta(minutes=30), "чай"),
        ("через минуту напомни о звонке", timedelta(minutes=1), "звонке"),
        ("через неделю напомни продлить домен", timedelta(days=7), "продлить домен"),
        ("напомни через 20m проверить деплой", timedelta(minutes=20), "проверить деплой"),
    ],
)
def test_relative_in_text(text, delta, task, now):
    r = parse_text_reminder(text, now)
    assert r.remind_at == now + delta
    assert r.task == task


def test_day_word_with_time_wins_over_bare_time(now):
    """E3: «завтра в 9:00» целиком, а не только «9:00»."""
    r = parse_text_reminder("завтра в 9:00 напомни позвонить маме", now)
    assert r.remind_at == datetime(2026, 11, 9, 9, 0, tzinfo=TZ)
    assert r.task == "позвонить маме"


def test_absolute_date_in_text(now):
    """E4."""
    r = parse_text_reminder("напомни мне что надо оплатить интернет 2026-11-20 10:00", now)
    assert r.remind_at == datetime(2026, 11, 20, 10, 0, tzinfo=TZ)
    assert r.task == "надо оплатить интернет"


def test_bare_time_with_v(now):
    r = parse_text_reminder("напомни в 18:30 забрать посылку", now)
    assert r.remind_at == datetime(2026, 11, 8, 18, 30, tzinfo=TZ)
    assert r.task == "забрать посылку"


def test_time_only_task_empty(now):
    """E7."""
    r = parse_text_reminder("напомни через 5 минут", now)
    assert r.remind_at == now + timedelta(minutes=5)
    assert r.task == ""


def test_past_time_in_text_raises(now):
    """E8."""
    with pytest.raises(ValueError, match="прошл"):
        parse_text_reminder("сегодня 10:00 напомни выпить кофе", now)


def test_no_time_found(now):
    """E9."""
    r = parse_text_reminder("напомни купить хлеб", now)
    assert r.remind_at is None
    assert r.task == "купить хлеб"


def test_task_case_and_html_preserved(now):
    r = parse_text_reminder("через 2 мин напомни про <b>Claude</b> лимиты", now)
    assert r.task == "<b>Claude</b> лимиты"


def test_text_without_trigger_keeps_words(now):
    """В шаге /new слово «про» в начале задачи без «напомни» не трогаем."""
    r = parse_text_reminder("про лимиты клода через 2 минуты", now)
    assert r.task == "про лимиты клода"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("напомни купить хлеб", True),
        ("Напомни купить хлеб", True),
        ("напомните завтра", True),
        ("надо напомнить Пете", True),
        ("через 2 минуты напомни, пожалуйста", True),
        ("это напоминание о встрече", False),  # E10
        ("он мне напомнил", False),
        ("встреча через 2 часа", False),
        ("", False),
    ],
)
def test_has_trigger(text, expected):
    assert has_trigger(text) is expected


def test_clean_task_variants():
    assert clean_task("напомни мне про лимиты клода") == "лимиты клода"
    assert clean_task("напомни об оплате") == "оплате"
    assert clean_task("напомни чтобы полить цветы") == "полить цветы"
    assert clean_task("  ,  напомни ,  , выключить духовку ") == "выключить духовку"
    assert clean_task("купить хлеб") == "купить хлеб"


# --- extract_time напрямую ---------------------------------------------------------


def test_extract_returns_remainder(now):
    dt, rest = extract_time("через 2 минуты напомни про лимиты клода", now)
    assert dt == now + timedelta(minutes=2)
    assert rest == "напомни про лимиты клода"


def test_extract_none_when_absent(now):
    assert extract_time("купить хлеб", now) is None


def test_extract_ignores_invalid_clock(now):
    """«25:00» — не время; ищем дальше / возвращаем None."""
    assert extract_time("счёт 25:00 матча", now) is None


def test_extract_does_not_match_inside_words(now):
    """«через 2 мамы» — не «через 2 м»."""
    assert extract_time("приду через 2 мамы", now) is None


# --- По итогам ревью ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "напомни посмотреть https://youtu.be/x?t=12:30",
        "напомни посмотреть youtu.be/x?t=12:30",
        "напомни глянуть www.example.com/a?at=10:15",
    ],
)
def test_time_inside_url_ignored(text, now):
    """Таймкод в ссылке — не время напоминания, ссылка не портится."""
    r = parse_text_reminder(text, now)
    assert r.remind_at is None
    assert r.task == text.removeprefix("напомни ")


def test_url_kept_when_time_elsewhere(now):
    r = parse_text_reminder("напомни посмотреть https://youtu.be/x?t=12:30 через 2 часа", now)
    assert r.remind_at == now + timedelta(hours=2)
    assert r.task == "посмотреть https://youtu.be/x?t=12:30"


def test_o_tom_chto_stripped(now):
    r = parse_text_reminder("напомни мне о том что завтра в 10:00 экзамен", now)
    assert r.task == "экзамен"
    assert clean_task("напомни о том, что надо полить цветы") == "надо полить цветы"


def test_decimal_comma_not_mangled():
    assert clean_task("купить 1,5 л молока") == "купить 1,5 л молока"
    assert clean_task("напомни через 1,5 часа") == "через 1,5 часа"
