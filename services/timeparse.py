"""Разбор времени напоминаний и расчёт повторов. Без Telegram, всё — aware datetime."""

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum


class Repeat(StrEnum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"


REPEAT_LABELS = {Repeat.NONE: "один раз", Repeat.DAILY: "ежедневно", Repeat.WEEKLY: "еженедельно"}
_STEP_DAYS = {Repeat.DAILY: 1, Repeat.WEEKLY: 7}

FORMATS_HELP = (
    "Форматы: HH:MM, HH:MM:SS, YYYY-MM-DD HH:MM[:SS], "
    "сегодня / завтра / послезавтра HH:MM, "
    "через 20 мин / через 2 ч / через 1 д / через 1 ч 30 мин (или 20m, 2h, 1h30m)."
)
INVALID_MSG = f"Не понял время. {FORMATS_HELP}"
PAST_MSG = "Это время уже прошло — укажи время в будущем."

_ABS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
_DAY_RE = re.compile(r"(сегодня|завтра|послезавтра)\s+(?:в\s+)?(\S+)")
_DAY_OFFSETS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

_UNIT = r"(минут[уыа]?|мин|м|часов|часа|час|ч|дней|дня|день|дн|д)"
_PART_RE = re.compile(rf"(\d+)\s*{_UNIT}")
_REL_RU_RE = re.compile(rf"через\s+((?:\d+\s*{_UNIT}\s*)+)")
_REL_EN_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?")


def _unit_delta(amount: int, unit: str) -> timedelta:
    if unit.startswith(("м",)):
        return timedelta(minutes=amount)
    if unit.startswith(("ч",)):
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _relative_delta(s: str) -> timedelta | None:
    if m := _REL_RU_RE.fullmatch(s):
        return sum((_unit_delta(int(n), u) for n, u in _PART_RE.findall(m.group(1))), timedelta())
    if (m := _REL_EN_RE.fullmatch(s)) and any(m.groups()):
        d, h, mi = (int(x or 0) for x in m.groups())
        return timedelta(days=d, hours=h, minutes=mi)
    return None


def _parse_clock(s: str) -> time | None:
    m = _TIME_RE.fullmatch(s)
    if not m:
        return None
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or sec > 59:
        return None
    return time(h, mi, sec)


def parse_reminder_time(text: str, now: datetime) -> datetime:
    """Текст → aware datetime в часовом поясе `now`. Прошлое и мусор → ValueError."""
    tz = now.tzinfo
    if tz is None:
        raise ValueError("now must be timezone-aware")
    now = now.replace(microsecond=0)
    s = " ".join((text or "").strip().lower().split())
    if not s:
        raise ValueError(INVALID_MSG)

    if s.startswith("через") or _REL_EN_RE.fullmatch(s):
        delta = _relative_delta(s)
        if not delta or delta <= timedelta(0):
            raise ValueError(INVALID_MSG)
        # Реальное прошедшее время: считаем в UTC, чтобы переход на летнее/зимнее не сбивал.
        return (now.astimezone(UTC) + delta).astimezone(tz)

    if m := _DAY_RE.fullmatch(s):
        clock = _parse_clock(m.group(2))
        if clock is None:
            raise ValueError(INVALID_MSG)
        result = datetime.combine(now.date() + timedelta(days=_DAY_OFFSETS[m.group(1)]), clock, tz)
        if result <= now:
            raise ValueError(PAST_MSG)
        return result

    if (clock := _parse_clock(s)) is not None:
        result = datetime.combine(now.date(), clock, tz)
        if result <= now:
            result = datetime.combine(now.date() + timedelta(days=1), clock, tz)
        return result

    for fmt in _ABS_FORMATS:
        try:
            result = datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
        if result <= now:
            raise ValueError(PAST_MSG)
        return result

    raise ValueError(INVALID_MSG)


def _local_parts(dt: datetime, tz: tzinfo | None) -> tuple[date, time, tzinfo]:
    zone = tz or dt.tzinfo
    local = dt.astimezone(zone)
    return local.date(), local.time(), zone


def shift(dt: datetime, repeat: Repeat, n: int, tz: tzinfo | None = None) -> datetime:
    """Сдвиг на n шагов повтора по местному времени (9:00 остаётся 9:00 при смене DST)."""
    d, clock, zone = _local_parts(dt, tz)
    return datetime.combine(d + timedelta(days=_STEP_DAYS[repeat] * n), clock, zone)


def next_occurrence(
    base: datetime, repeat: Repeat, now: datetime, tz: tzinfo | None = None
) -> datetime:
    """Первое вхождение серии (base, base+шаг, …), которое строго позже now."""
    if repeat is Repeat.NONE:
        raise ValueError("one-off reminder has no next occurrence")
    d, clock, zone = _local_parts(base, tz)
    step = _STEP_DAYS[repeat]
    candidate = datetime.combine(d, clock, zone)
    if candidate <= now:
        skipped = (now.astimezone(zone).date() - d).days // step
        d += timedelta(days=step * max(skipped, 0))
        candidate = datetime.combine(d, clock, zone)
        while candidate <= now:
            d += timedelta(days=step)
            candidate = datetime.combine(d, clock, zone)
    return candidate


def format_dt(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")
