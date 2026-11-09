"""Этап 3: таблица notes (id для кнопок), связь с digests и link_jobs."""

import sqlite3

from database import Database
from tests.conftest import OWNER


def test_add_and_get_note(db):
    nid = db.add_note("2026-11-09 Хоткеи", "https://www.youtube.com/watch?v=x", "youtube", OWNER)
    note = db.get_note(nid)
    assert note.id == nid
    assert note.note_name == "2026-11-09 Хоткеи"
    assert note.url == "https://www.youtube.com/watch?v=x"
    assert note.source == "youtube"
    assert note.chat_id == OWNER
    assert note.detailed is False and note.read is False
    assert db.get_note(nid + 100) is None


def test_find_note_by_any_key(db):
    nid = db.add_note("N", "https://a", "video", OWNER)
    db.save_digest(["k1", "k2"], "N", "https://a", "video", note_id=nid)
    assert db.find_note(["zzz", "k2"]).id == nid
    assert db.find_note(["zzz"]) is None
    assert db.find_digest(["k1"]) == "N"  # старый API не сломан


def test_note_id_for_old_digest_creates_note(db):
    """Конспект до этапа 3 (без note_id) — при «Уже есть» получает строку notes, один раз."""
    db.save_digest(["k1", "k2"], "Old", "https://a", "article")
    nid = db.note_id_for(["k2"], OWNER)
    assert nid is not None
    assert db.get_note(nid).note_name == "Old"
    assert db.note_id_for(["k1"], OWNER) == nid
    assert db.note_id_for(["nope"], OWNER) is None


def test_mark_detailed_only_once(db):
    nid = db.add_note("N", "https://a", "video", OWNER)
    assert db.mark_detailed(nid) is True
    assert db.mark_detailed(nid) is False  # второе нажатие
    assert db.get_note(nid).detailed is True
    db.mark_detailed(nid, False)  # подробный конспект не удался — можно снова
    assert db.mark_detailed(nid) is True


def test_mark_read_and_rename(db):
    nid = db.add_note("N", "https://a", "video", OWNER)
    db.mark_read(nid)
    db.rename_note(nid, "N2")
    note = db.get_note(nid)
    assert note.read is True and note.note_name == "N2"


def test_delete_note_frees_url(db):
    nid = db.add_note("N", "https://a", "video", OWNER)
    db.save_digest(["k1", "k2"], "N", "https://a", "video", note_id=nid)
    other = db.add_note("M", "https://b", "video", OWNER)
    db.save_digest(["k3"], "M", "https://b", "video", note_id=other)
    db.delete_note(nid)
    assert db.get_note(nid) is None
    assert db.find_digest(["k1", "k2"]) is None
    assert db.find_digest(["k3"]) == "M"


def test_job_keeps_note_id(db):
    jid = db.add_job("https://a", "подробно", True, OWNER, 10, note_id=7)
    plain = db.add_job("https://b", "", False, OWNER, 11)
    rows = {r.id: r for r in db.pending_jobs()}
    assert rows[jid].note_id == 7
    assert rows[plain].note_id is None


def test_migration_adds_columns_to_old_db(tmp_path):
    """База этапа 2 (без notes, note_id) открывается и получает новые колонки."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE digests (key TEXT PRIMARY KEY, note_name TEXT NOT NULL, url TEXT NOT NULL,
                              source TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE link_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, hint TEXT NOT NULL DEFAULT '',
            force INTEGER NOT NULL DEFAULT 0, chat_id INTEGER NOT NULL,
            status_msg_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO digests VALUES ('k', 'Old', 'https://a', 'article', '2026-11-01T00:00:00');
        """
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.init()
    assert db.note_id_for(["k"], OWNER) is not None
    jid = db.add_job("https://a", "", False, OWNER, 1, note_id=3)
    assert db.pending_jobs()[0].id == jid


def test_r1_other_note_names(db):
    a = db.add_note("A", "https://u", "video", OWNER)
    db.add_note("B", "https://u", "video", OWNER)
    db.add_note("C", "https://other", "video", OWNER)
    db.save_digest(["k"], "Legacy", "https://u", "video")
    assert db.other_note_names(a) == {"B", "Legacy"}


def test_r2_mark_read_false(db):
    nid = db.add_note("N", "https://a", "video", OWNER)
    db.mark_read(nid)
    db.mark_read(nid, False)
    assert db.get_note(nid).read is False


def test_r5_note_id_for_uses_given_url(db):
    db.save_digest(["youtube:X"], "Old", "https://youtu.be/X?si=1", "youtube")
    nid = db.note_id_for(["youtube:X"], OWNER, url="https://www.youtube.com/watch?v=X")
    assert db.get_note(nid).url == "https://www.youtube.com/watch?v=X"
