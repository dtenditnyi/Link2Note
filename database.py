"""SQLite: напоминания, конспекты (дубликаты и кнопки) и очередь ссылок Link2Note.

Время хранится в UTC ISO-строкой — сортируется и сравнивается корректно.
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

-- Один конспект может иметь несколько ключей (normalize ссылки + ID от yt-dlp).
CREATE TABLE IF NOT EXISTS digests (
    key        TEXT PRIMARY KEY,
    note_name  TEXT NOT NULL,
    url        TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS link_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT    NOT NULL,
    hint          TEXT    NOT NULL DEFAULT '',
    force         INTEGER NOT NULL DEFAULT 0,
    chat_id       INTEGER NOT NULL,
    status_msg_id INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts      INTEGER NOT NULL DEFAULT 0,          -- сколько раз воркер брался за задачу
    created_at    TEXT    NOT NULL,
    run_after     TEXT,                                -- UTC; отложена до этого времени
    defers        INTEGER NOT NULL DEFAULT 0,          -- откладываний из-за перегрузки Gemini
    quota_waits   INTEGER NOT NULL DEFAULT 0           -- ожиданий сброса дневной квоты
);
CREATE INDEX IF NOT EXISTS idx_link_jobs_status ON link_jobs (status, id);

-- Заметка, на которую ссылаются кнопки под конспектом (id влезает в callback_data).
-- Правда о «прочитано» — во frontmatter файла; read здесь — только для вида кнопок.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_name  TEXT    NOT NULL,
    url        TEXT    NOT NULL,                -- url из заметки (для поиска переименованной)
    source     TEXT    NOT NULL,
    chat_id    INTEGER NOT NULL,
    detailed   INTEGER NOT NULL DEFAULT 0,      -- «Подробнее» уже нажата
    read       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
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


@dataclass(frozen=True, slots=True)
class LinkJob:
    id: int
    url: str
    hint: str
    force: bool
    chat_id: int
    status_msg_id: int
    attempts: int
    run_after: datetime | None = None  # aware, UTC
    defers: int = 0
    quota_waits: int = 0
    note_id: int | None = None  # «Подробнее»: заменить эту заметку


@dataclass(frozen=True, slots=True)
class NoteRow:
    id: int
    note_name: str
    url: str
    source: str
    chat_id: int
    detailed: bool
    read: bool


_NOTE_COLUMNS = "id, note_name, url, source, chat_id, detailed, read"


def _note_row(r: tuple) -> NoteRow:
    return NoteRow(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]))


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Колонки, добавленные после первого выпуска таблицы (CREATE TABLE IF NOT EXISTS их не добавит).
_LINK_JOBS_ADDED = {
    "run_after": "TEXT",
    "defers": "INTEGER NOT NULL DEFAULT 0",
    "quota_waits": "INTEGER NOT NULL DEFAULT 0",
    "note_id": "INTEGER",
}
_DIGESTS_ADDED = {"note_id": "INTEGER"}


def _migrate(c: sqlite3.Connection) -> None:
    for table, added in (("link_jobs", _LINK_JOBS_ADDED), ("digests", _DIGESTS_ADDED)):
        existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        for column, decl in added.items():
            if column not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            _migrate(c)

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

    # --- Конспекты: поиск дубликатов ------------------------------------------------------

    def find_digest(self, keys: list[str]) -> str | None:
        """Имя заметки по любому из ключей (последняя сохранённая), иначе None."""
        if not keys:
            return None
        marks = ", ".join("?" * len(keys))
        with self._conn() as c:
            row = c.execute(
                f"SELECT note_name FROM digests WHERE key IN ({marks}) "
                "ORDER BY created_at DESC LIMIT 1",
                keys,
            ).fetchone()
        return row[0] if row else None

    def save_digest(
        self, keys: list[str], note_name: str, url: str, source: str, *, note_id: int | None = None
    ) -> None:
        now = _now()
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO digests (key, note_name, url, source, created_at, note_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(k, note_name, url, source, now, note_id) for k in dict.fromkeys(keys)],
            )

    # --- Заметки: кнопки под конспектом ---------------------------------------------------

    def add_note(self, note_name: str, url: str, source: str, chat_id: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO notes (note_name, url, source, chat_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (note_name, url, source, chat_id, _now()),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def get_note(self, note_id: int) -> NoteRow | None:
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        return _note_row(row) if row else None

    def find_note(self, keys: list[str]) -> NoteRow | None:
        """Заметка по любому ключу ссылки (последняя сохранённая)."""
        if not keys:
            return None
        marks = ", ".join("?" * len(keys))
        with self._conn() as c:
            row = c.execute(
                f"SELECT note_id FROM digests WHERE key IN ({marks}) AND note_id IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                keys,
            ).fetchone()
        return self.get_note(row[0]) if row else None

    def note_id_for(self, keys: list[str], chat_id: int, *, url: str | None = None) -> int | None:
        """id заметки по ключам; конспекту до этапа 3 (без note_id) строка notes создаётся.

        url — ссылка, записанная в заметку (у YouTube каноническая); в digests лежит сырая.
        """
        if not keys:
            return None
        marks = ", ".join("?" * len(keys))
        with self._conn() as c:
            row = c.execute(
                f"SELECT note_id, note_name, url, source FROM digests WHERE key IN ({marks}) "
                "ORDER BY created_at DESC LIMIT 1",
                keys,
            ).fetchone()
            if row is None:
                return None
            note_id, note_name, digest_url, source = row
            if note_id is not None:
                return note_id
            cur = c.execute(
                "INSERT INTO notes (note_name, url, source, chat_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (note_name, url or digest_url, source, chat_id, _now()),
            )
            note_id = cur.lastrowid
            assert note_id is not None
            c.execute(
                "UPDATE digests SET note_id = ? WHERE note_name = ? AND note_id IS NULL",
                (note_id, note_name),
            )
            return note_id

    def mark_detailed(self, note_id: int, value: bool = True) -> bool:
        """True — флаг изменился (второе нажатие «Подробнее» вернёт False)."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE notes SET detailed = ? WHERE id = ? AND detailed = ?",
                (int(value), note_id, int(not value)),
            )
            return cur.rowcount > 0

    def mark_read(self, note_id: int, value: bool = True) -> None:
        with self._conn() as c:
            c.execute("UPDATE notes SET read = ? WHERE id = ?", (int(value), note_id))

    def other_note_names(self, note_id: int) -> set[str]:
        """Имена других заметок с тем же url («заново»): их нельзя принять за эту при поиске."""
        with self._conn() as c:
            row = c.execute("SELECT note_name, url FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                return set()
            name, url = row
            rows = c.execute(
                "SELECT note_name FROM notes WHERE url = ? AND id != ? "
                "UNION SELECT note_name FROM digests WHERE url = ? "
                "AND (note_id IS NULL OR note_id != ?)",
                (url, note_id, url, note_id),
            ).fetchall()
        return {r[0] for r in rows} - {name}

    def rename_note(self, note_id: int, note_name: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE notes SET note_name = ? WHERE id = ?", (note_name, note_id))
            c.execute("UPDATE digests SET note_name = ? WHERE note_id = ?", (note_name, note_id))

    def delete_note(self, note_id: int) -> None:
        """Заметка удалена — её ссылки снова можно прислать на конспект."""
        with self._conn() as c:
            c.execute("DELETE FROM digests WHERE note_id = ?", (note_id,))
            c.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    # --- Очередь ссылок ---------------------------------------------------------------------

    def add_job(
        self,
        url: str,
        hint: str,
        force: bool,
        chat_id: int,
        status_msg_id: int,
        *,
        note_id: int | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO link_jobs "
                "(url, hint, force, chat_id, status_msg_id, created_at, note_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, hint, int(force), chat_id, status_msg_id, _now(), note_id),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def start_job(self, job_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE link_jobs SET attempts = attempts + 1 WHERE id = ?", (job_id,))

    def postpone_job(self, job_id: int, run_after: datetime, *, daily: bool) -> None:
        """Gemini перегружен: задача ждёт до run_after. Это не падение — попытку возвращаем."""
        counter = "quota_waits" if daily else "defers"
        with self._conn() as c:
            c.execute(
                f"UPDATE link_jobs SET run_after = ?, {counter} = {counter} + 1, "
                "attempts = MAX(attempts - 1, 0) WHERE id = ?",
                (_to_db(run_after), job_id),
            )

    def release_job(self, job_id: int) -> None:
        """Задачу прервали штатно (остановка бота) — попытку не засчитываем."""
        with self._conn() as c:
            c.execute(
                "UPDATE link_jobs SET attempts = MAX(attempts - 1, 0) WHERE id = ?", (job_id,)
            )

    def finish_job(self, job_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE link_jobs SET status = ? WHERE id = ?", (status, job_id))

    def pending_jobs(self) -> list[LinkJob]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, url, hint, force, chat_id, status_msg_id, attempts, "
                "run_after, defers, quota_waits, note_id "
                "FROM link_jobs WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [
            LinkJob(
                r[0],
                r[1],
                r[2],
                bool(r[3]),
                r[4],
                r[5],
                r[6],
                datetime.fromisoformat(r[7]) if r[7] else None,
                r[8],
                r[9],
                r[10],
            )  # fmt: skip
            for r in rows
        ]
