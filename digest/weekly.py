"""Воскресный дайджест непрочитанного: вс 18:00 — список самых старых непрочитанных конспектов.

Правда о «прочитано» — frontmatter файлов в хранилище (после git pull), а не база: заметка,
отмеченная `status: read` прямо в Obsidian, в дайджест не попадёт. Gemini не вызывается.
"""

import logging
from html import escape

from digest.vault_base import UnreadNote, VaultBackend

log = logging.getLogger(__name__)

JOB_ID = "weekly-digest"
LIMIT = 5


def unread_text(notes: list[UnreadNote], limit: int = LIMIT) -> str | None:
    """None — непрочитанного нет. notes уже отсортированы: самые старые первыми."""
    if not notes:
        return None
    lines = [f"📚 <b>Непрочитанных конспектов: {len(notes)}</b>", ""]
    for i, note in enumerate(notes[:limit], 1):
        when = f" · {note.date_added:%d.%m}" if note.date_added else ""
        lines.append(f"{i}. {escape(note.title)} — {note.read_minutes} мин{when}")
    if len(notes) > limit:
        lines += ["", f"…и ещё {len(notes) - limit}"]
    return "\n".join(lines)


async def send_weekly(*, bot, vault: VaultBackend, chat_id: int) -> None:
    """Задача планировщика: ошибки только в лог — следующая неделя всё равно наступит."""
    try:
        text = unread_text(await vault.unread())
        if text is None:
            log.info("Воскресный дайджест: всё прочитано, не отправляю")
            return
        await bot.send_message(chat_id, text)
    except Exception:
        log.exception("Воскресный дайджест не отправлен")


def schedule_weekly(aps, *, bot, vault: VaultBackend, chat_id: int) -> None:
    """Вс 18:00 в часовом поясе планировщика (TZ из config)."""
    aps.add_job(
        send_weekly,
        "cron",
        id=JOB_ID,
        day_of_week="sun",
        hour=18,
        minute=0,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,  # event loop был занят — пришлём с опозданием, но один раз
        kwargs={"bot": bot, "vault": vault, "chat_id": chat_id},
    )
