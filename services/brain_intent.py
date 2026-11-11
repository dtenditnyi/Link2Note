"""Голосовые команды для сайта Second Brain без Gemini (DECISIONS D11).

«Добавь задачу купить молоко» → ("task", "купить молоко"); «Запиши в заметки идея…» /
«запиши во входящие …» → ("note", "идея…"). Иначе None — голосовое разбирается как напоминание.
"""

import re

_TASK_RE = re.compile(
    r"^\s*(?:добавь|добавить|создай|запиши)\s+(?:мне\s+)?(?:новую\s+)?задачу[\s,:.—-]*", re.I
)
_NOTE_RE = re.compile(
    r"^\s*(?:запиши|запомни|добавь|сохрани)\s+(?:мне\s+)?"
    r"(?:в\s+заметк[иу]|во\s+входящие|в\s+инбокс|заметку)[\s,:.—-]*",
    re.I,
)


def voice_command(text: str) -> tuple[str, str] | None:
    for kind, pattern in (("task", _TASK_RE), ("note", _NOTE_RE)):
        m = pattern.match(text or "")
        if m:
            rest = text[m.end() :].strip().rstrip(".")
            return (kind, rest) if rest else None
    return None
