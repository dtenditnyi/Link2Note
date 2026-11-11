"""Разбор `/task`: срок («завтра», «до пятницы», «до 15.11», «через 3 дня»), «!» → приоритет,
`#тег` → теги (только разрешённые в хранилище). Время суток не нужно — у задачи только дата.

Сначала свои шаблоны дат, потом общий парсер времени напоминаний (`extract_time`) —
он понимает «через 2 дня», «завтра в 9:00» и т.п.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from digest.llm import ALLOWED_TAGS
from services.timeparse import extract_time

_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0,
    "вторник": 1, "вторника": 1,
    "среда": 2, "среду": 2, "среды": 2,
    "четверг": 3, "четверга": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4,
    "суббота": 5, "субботу": 5, "субботы": 5,
    "воскресенье": 6, "воскресенья": 6,
}  # fmt: skip
_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_NB = r"(?![а-яё\w])"
_PRE = r"(?:(?:до|к|в|во|на)\s+)?"
_DAY_RE = re.compile(rf"(?<![а-яё\w]){_PRE}({'|'.join(_DAYS)}){_NB}", re.I)
# День недели и «15.11» — срок только с предлогом («до пятницы», «в среду», «к 15.11»):
# иначе «настроить среду разработки» и «python 3.12» стали бы сроками. С годом — и без предлога.
_WEEKDAY_RE = re.compile(rf"(?<![а-яё\w])(?:до|к|в|во|на|по)\s+({'|'.join(_WEEKDAYS)}){_NB}", re.I)
_DATE_RE = re.compile(
    r"(?<![\w.])(?:(?:до|к|на)\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?"
    r"|(\d{1,2})\.(\d{1,2})\.(\d{4}))(?![\w.])",
    re.I,
)
_TAG_RE = re.compile(r"(?<![\w#])#([\w-]+)")
_PRIORITY_RE = re.compile(r"(?<!\S)!{1,3}(?!\S)")


@dataclass
class TaskDraft:
    text: str
    due: date | None = None
    priority: str | None = None  # high | None (сайт ставит normal)
    tags: list[str] = field(default_factory=list)
    unknown_tags: list[str] = field(default_factory=list)


def _cut(text: str, m: re.Match) -> str:
    return f"{text[: m.start()]} {text[m.end() :]}"


def _date_from(m: re.Match, today: date) -> date | None:
    g = m.groups()
    day_s, month_s, year = (g[0], g[1], g[2]) if g[0] else (g[3], g[4], g[5])
    day, month = int(day_s), int(month_s)
    y = int(year) + (2000 if year and len(year) == 2 else 0) if year else today.year
    try:
        due = date(y, month, day)
    except ValueError:
        return None
    if not year and due < today:
        due = due.replace(year=due.year + 1)
    return due


def parse_task(raw: str, now: datetime) -> TaskDraft:
    text = raw
    today = now.date()
    tags: list[str] = []
    unknown: list[str] = []
    for tag in _TAG_RE.findall(text):
        tag = tag.lower()
        bucket = tags if tag in ALLOWED_TAGS else unknown
        if tag not in bucket:
            bucket.append(tag)
    text = _TAG_RE.sub(" ", text)

    priority = None
    if _PRIORITY_RE.search(text):
        priority = "high"
        text = _PRIORITY_RE.sub(" ", text)

    due: date | None = None
    if m := _DATE_RE.search(text):
        due = _date_from(m, today)
        if due is not None:
            text = _cut(text, m)
    if due is None and (m := _DAY_RE.search(text)):
        due = today + timedelta(days=_DAYS[m.group(1).lower()])
        text = _cut(text, m)
    if due is None and (m := _WEEKDAY_RE.search(text)):
        ahead = (_WEEKDAYS[m.group(1).lower()] - today.weekday()) % 7 or 7
        due = today + timedelta(days=ahead)
        text = _cut(text, m)
    if due is None:
        try:
            found = extract_time(text, now)
        except ValueError:  # время в прошлом — срок не ставим, текст оставляем как есть
            found = None
        if found is not None:
            when, text = found
            due = when.date()

    text = " ".join(text.split()).strip(" ,.;:—-")
    return TaskDraft(text, due, priority, tags[:3], unknown)
