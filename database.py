"""SQLite: напоминания. Время хранится в UTC ISO-строкой — сортируется и сравнивается корректно.

Таблица digests появится на этапе 2.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from services.timeparse import Repeat

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    task       TEXT    NOT NULL,
    remind_at  TEXT    NOT NULL,              -- UTC ISO 8601
    repeat     TEXT    NOT NULL DEFAULT 'none',
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending | sent | done | missed | failed
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_status_time ON reminders (status, remind_at);
"""
_COLUMNS = "id, chat_id, task, remind_at, repeat, status"


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int
    chat_id: int
    task: str
    remind_at: datetime  # aware, UTC
    repeat: Repeat
    status: str


def _to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("remind_at must be timezone-aware")
    return dt.astimezone(UTC).isoformat()


def _row(row: tuple) -> Reminder:
    rid, chat_id, task, remind_at, repeat, status = row
    return Reminder(rid, chat_id, task, datetime.fromisoformat(remind_at), Repeat(repeat), status)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def add(
        self, chat_id: int, task: str, remind_at: datetime, repeat: Repeat = Repeat.NONE
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders (chat_id, task, remind_at, repeat, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, task, _to_db(remind_at), repeat.value, datetime.now(UTC).isoformat()),
            )
            return cur.lastrowid

    def get(self, reminder_id: int, chat_id: int) -> Reminder | None:
        """Только напоминание своего чата — для всего, что приходит из Telegram."""
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_COLUMNS} FROM reminders WHERE id = ? AND chat_id = ?",
                (reminder_id, chat_id),
            ).fetchone()
        return _row(row) if row else None

    def get_by_id(self, reminder_id: int) -> Reminder | None:
        """Для планировщика (без проверки чата)."""
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_COLUMNS} FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return _row(row) if row else None

    def list_pending(self, chat_id: int) -> list[Reminder]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_COLUMNS} FROM reminders WHERE chat_id = ? AND status = 'pending' "
                "ORDER BY remind_at, id",
                (chat_id,),
            ).fetchall()
        return [_row(r) for r in rows]

    def all_pending(self) -> list[Reminder]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_COLUMNS} FROM reminders WHERE status = 'pending' ORDER BY remind_at, id"
            ).fetchall()
        return [_row(r) for r in rows]

    def update_time(
        self, reminder_id: int, chat_id: int, remind_at: datetime, status: str = "pending"
    ) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET remind_at = ?, status = ? WHERE id = ? AND chat_id = ?",
                (_to_db(remind_at), status, reminder_id, chat_id),
            )
            return cur.rowcount > 0

    def set_status(self, reminder_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))

    def finish(self, reminder_id: int, status: str, expected_remind_at: datetime) -> bool:
        """pending → status, только если напоминание не меняли с момента чтения."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET status = ? "
                "WHERE id = ? AND status = 'pending' AND remind_at = ?",
                (status, reminder_id, _to_db(expected_remind_at)),
            )
            return cur.rowcount > 0

    def advance(
        self, reminder_id: int, expected_remind_at: datetime, new_remind_at: datetime
    ) -> bool:
        """Сдвиг повторяющегося на следующее вхождение — тоже только если его не меняли."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET remind_at = ? "
                "WHERE id = ? AND status = 'pending' AND remind_at = ?",
                (_to_db(new_remind_at), reminder_id, _to_db(expected_remind_at)),
            )
            return cur.rowcount > 0

    def delete(self, reminder_id: int, chat_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM reminders WHERE id = ? AND chat_id = ?", (reminder_id, chat_id)
            )
            return cur.rowcount > 0
