"""Текст напоминания: «через 2 минуты напомни про лимиты клода» → время + «лимиты клода»."""

import re
from dataclasses import dataclass
from datetime import datetime

from services.timeparse import extract_time

_NB_BEFORE = r"(?<![а-яё])"
_NB_AFTER = r"(?![а-яё])"
_TRIGGER = rf"{_NB_BEFORE}напомн(?:и|ить|ите){_NB_AFTER}"
TRIGGER_RE = re.compile(_TRIGGER, re.IGNORECASE)

# «напомни», затем необязательно «мне/нам» и «про/о/об/что/чтобы» — всё это не часть задачи.
_TRIGGER_PHRASE_RE = re.compile(
    rf"{_TRIGGER}[\s,.:;!—-]*"
    rf"(?:(?:мне|нам){_NB_AFTER}[\s,]*)?"
    rf"(?:(?:о\s+том,?\s+(?:чтобы|что)|про|о|об|что|чтобы){_NB_AFTER}\s*)?",
    re.IGNORECASE,
)
_EDGE_PUNCT = " ,.;:!—-"


@dataclass(frozen=True)
class TextReminder:
    task: str
    remind_at: datetime | None


def has_trigger(text: str | None) -> bool:
    return bool(TRIGGER_RE.search(text or ""))


def clean_task(text: str) -> str:
    """Убрать «напомни (мне) (про …)» и лишние знаки по краям; остальное — как написано."""
    s = _TRIGGER_PHRASE_RE.sub(" ", text, count=1)
    s = re.sub(r"\s*[,;](?:\s*[,;])+\s*", ", ", s)  # «, ,» после вырезания → одна запятая
    return " ".join(s.split()).strip(_EDGE_PUNCT).strip()


def parse_text_reminder(text: str, now: datetime) -> TextReminder:
    """Время (если есть) + задача. Время в прошлом → ValueError."""
    found = extract_time(text, now)
    if found is None:
        return TextReminder(clean_task(text), None)
    when, rest = found
    return TextReminder(clean_task(rest), when)
