"""F3: digest.admin — очередь и онлайн-бэкап для deploy-скриптов."""

import gzip
import sqlite3
from datetime import UTC, datetime

from digest.admin import main

T = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def test_queue_counts(db, capsys):
    db.add_op("task", "g1", {"text": "a"}, "k1", T)
    done = db.add_op("task", "g2", {"text": "b"}, "k2", T)
    db.update_op(done, T, status="done")
    assert main(["--db", str(db.path), "queue"]) == 0
    out = capsys.readouterr().out
    assert "pending: 1" in out and "done: 1" in out and "oldest_pending: 2026-11-11" in out


def test_queue_missing_db(tmp_path):
    assert main(["--db", str(tmp_path / "nope.db"), "queue"]) == 1


def test_backup_gz_while_open(db, tmp_path):
    db.add(1, "напомнить", T)
    holder = sqlite3.connect(db.path)  # бот держит базу открытой
    holder.execute("BEGIN")
    holder.execute("INSERT INTO reminders (chat_id, task, remind_at, created_at) "
                   "VALUES (1, 'в транзакции', 'x', 'x')")  # fmt: skip
    dst = tmp_path / "b" / "link2note.db.gz"
    assert main(["--db", str(db.path), "backup", str(dst)]) == 0
    holder.rollback()
    holder.close()
    raw = tmp_path / "restored.db"
    raw.write_bytes(gzip.decompress(dst.read_bytes()))
    with sqlite3.connect(raw) as c:
        assert [r[0] for r in c.execute("SELECT task FROM reminders")] == ["напомнить"]


def test_backup_missing_db(tmp_path):
    assert main(["--db", str(tmp_path / "nope.db"), "backup", str(tmp_path / "x.db")]) == 1
