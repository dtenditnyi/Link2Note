import sqlite3
from datetime import UTC, datetime, timedelta

from services.timeparse import Repeat
from tests.conftest import OWNER, STRANGER, TZ


def _dt(h, m=0, day=8):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


def test_add_and_get(db):
    rid = db.add(OWNER, "Выпить воды", _dt(13))
    r = db.get(rid, OWNER)
    assert r.task == "Выпить воды"
    assert r.status == "pending"
    assert r.repeat is Repeat.NONE
    assert r.remind_at == _dt(13)
    assert r.remind_at.tzinfo is not None


def test_stored_as_utc_iso(db):
    rid = db.add(OWNER, "x", _dt(13))
    with sqlite3.connect(db.path) as conn:
        raw = conn.execute("SELECT remind_at FROM reminders WHERE id=?", (rid,)).fetchone()[0]
    assert raw == "2026-11-08T11:00:00+00:00"


def test_naive_datetime_rejected(db):
    import pytest

    with pytest.raises(ValueError):
        db.add(OWNER, "x", datetime(2026, 11, 8, 13, 0))


def test_get_other_chat_returns_none(db):
    """A2: чужой chat_id не видит напоминание."""
    rid = db.add(OWNER, "x", _dt(13))
    assert db.get(rid, STRANGER) is None


def test_list_pending_only_and_sorted(db):
    """S5: только pending, по времени (в т.ч. через смену смещения UTC)."""
    late = db.add(OWNER, "late", datetime(2027, 4, 1, 9, 0, tzinfo=TZ))  # летнее время +03
    early = db.add(OWNER, "early", _dt(13))
    sent = db.add(OWNER, "sent", _dt(14))
    db.set_status(sent, "sent")
    db.add(STRANGER, "other", _dt(15))
    assert [r.id for r in db.list_pending(OWNER)] == [early, late]


def test_all_pending_for_restore(db):
    a = db.add(OWNER, "a", _dt(13))
    b = db.add(STRANGER, "b", _dt(14))
    done = db.add(OWNER, "c", _dt(15))
    db.set_status(done, "done")
    assert {r.id for r in db.all_pending()} == {a, b}


def test_update_time_resets_pending(db):
    rid = db.add(OWNER, "x", _dt(13))
    db.set_status(rid, "sent")
    assert db.update_time(rid, OWNER, _dt(18)) is True
    r = db.get(rid, OWNER)
    assert (r.remind_at, r.status) == (_dt(18), "pending")


def test_update_time_other_chat_does_nothing(db):
    rid = db.add(OWNER, "x", _dt(13))
    assert db.update_time(rid, STRANGER, _dt(18)) is False
    assert db.get(rid, OWNER).remind_at == _dt(13)


def test_delete_own_and_foreign(db):
    rid = db.add(OWNER, "x", _dt(13))
    assert db.delete(rid, STRANGER) is False
    assert db.get(rid, OWNER) is not None
    assert db.delete(rid, OWNER) is True
    assert db.get(rid, OWNER) is None


def test_repeat_roundtrip(db):
    rid = db.add(OWNER, "x", _dt(9), Repeat.WEEKLY)
    assert db.get(rid, OWNER).repeat is Repeat.WEEKLY


def test_get_by_id_internal(db):
    rid = db.add(STRANGER, "x", _dt(9))
    assert db.get_by_id(rid).chat_id == STRANGER


def test_html_and_quotes_stored_verbatim(db):
    rid = db.add(OWNER, "<b>'; DROP TABLE reminders;--", _dt(9))
    assert db.get(rid, OWNER).task == "<b>'; DROP TABLE reminders;--"


def test_init_is_idempotent_and_creates_parent_dir(tmp_path):
    from database import Database

    d = Database(tmp_path / "nested" / "x.db")
    d.init()
    d.init()
    assert (tmp_path / "nested" / "x.db").exists()


def test_utc_input_equals_local(db):
    rid = db.add(OWNER, "x", datetime(2026, 11, 8, 11, 0, tzinfo=UTC))
    assert db.get(rid, OWNER).remind_at == _dt(13)
    assert db.get(rid, OWNER).remind_at - _dt(13) == timedelta(0)


# --- Этап 2: дубликаты и очередь ссылок ------------------------------------------------


def test_d1_digest_found_by_any_key(db):
    db.save_digest(["tiktok:1", "tiktok:abc"], "2026-11-09 Ролик", "https://t/1", "tiktok")
    assert db.find_digest(["tiktok:abc"]) == "2026-11-09 Ролик"
    assert db.find_digest(["other", "tiktok:1"]) == "2026-11-09 Ролик"
    assert db.find_digest(["unknown"]) is None
    assert db.find_digest([]) is None


def test_d1_resave_points_to_latest_note(db):
    db.save_digest(["k"], "old", "u", "article")
    db.save_digest(["k"], "new", "u", "article")
    assert db.find_digest(["k"]) == "new"


def test_d2_job_lifecycle(db):
    jid = db.add_job("https://a", "упор", True, OWNER, 10)
    [job] = db.pending_jobs()
    assert (job.id, job.url, job.hint, job.force, job.chat_id, job.status_msg_id) == (
        jid,
        "https://a",
        "упор",
        True,
        OWNER,
        10,
    )
    assert job.attempts == 0
    db.start_job(jid)
    db.start_job(jid)
    assert db.pending_jobs()[0].attempts == 2
    db.finish_job(jid, "done")
    assert db.pending_jobs() == []


def test_d2_pending_jobs_in_order(db):
    a = db.add_job("https://a", "", False, OWNER, 1)
    b = db.add_job("https://b", "", False, OWNER, 2)
    db.finish_job(a, "failed")
    c = db.add_job("https://c", "", False, OWNER, 3)
    assert [j.id for j in db.pending_jobs()] == [b, c]


def test_d2_init_on_old_db_adds_tables(tmp_path):
    from database import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,"
        " task TEXT NOT NULL, remind_at TEXT NOT NULL, repeat TEXT NOT NULL DEFAULT 'none',"
        " status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);"
        "INSERT INTO reminders (chat_id, task, remind_at, created_at)"
        " VALUES (1, 'старое', '2026-11-08T10:00:00+00:00', '2026-11-08T09:00:00+00:00');"
    )
    conn.commit()
    conn.close()
    old = Database(path)
    old.init()
    assert [r.task for r in old.all_pending()] == ["старое"]
    old.save_digest(["k"], "n", "u", "article")
    assert old.find_digest(["k"]) == "n"
    old.add_job("https://a", "", False, 1, 1)
    assert len(old.pending_jobs()) == 1
