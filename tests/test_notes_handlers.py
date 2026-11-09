"""Этап 3: кнопки под конспектом (B2–B7) и /unread."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from digest.errors import NoteNotFound, VaultError
from digest.vault import UnreadNote
from digest.worker import Job
from handlers.keyboards import NoteCb
from handlers.notes import DETAILED_HINT, cmd_unread, on_note
from tests.conftest import OWNER, make_callback, make_message, sent_texts
from tests.test_notes_worker import buttons

URL = "https://www.youtube.com/watch?v=abc"
NAME = "2026-11-09 Хоткеи"


def _vault():
    v = MagicMock()
    v.set_status = AsyncMock(return_value=NAME)
    v.delete = AsyncMock()
    v.unread = AsyncMock(return_value=[])
    return v


def _cb():
    cb = make_callback()
    cb.message.reply = AsyncMock(return_value=MagicMock(message_id=777))
    return cb


async def _press(db, action, nid, *, vault=None, queue=None):
    cb = _cb()
    vault = _vault() if vault is None else vault
    queue = asyncio.Queue() if queue is None else queue
    await on_note(
        cb, callback_data=NoteCb(action=action, id=nid), db=db, vault=vault, link_queue=queue
    )
    return cb, vault, queue


def _note(db, **kw):
    nid = db.add_note(NAME, URL, "youtube", OWNER)
    db.save_digest(["youtube:abc"], NAME, URL, "youtube", note_id=nid)
    return nid


def _markup(mock):
    return mock.call_args.kwargs["reply_markup"]


async def test_b2_read(db):
    nid = _note(db)
    cb, vault, _ = await _press(db, "read", nid)
    vault.set_status.assert_awaited_once_with(NAME, URL, "read", skip=set())
    assert db.get_note(nid).read is True
    assert [a for _, a, _ in buttons(_markup(cb.message.edit_reply_markup))] == ["more", "del"]
    cb.answer.assert_awaited_once()


async def test_b6_read_renamed_updates_name(db):
    nid = _note(db)
    vault = _vault()
    vault.set_status = AsyncMock(return_value="Мои хоткеи")
    await _press(db, "read", nid, vault=vault)
    assert db.get_note(nid).note_name == "Мои хоткеи"


async def test_b6_read_missing_note_db_unchanged(db):
    nid = _note(db)
    vault = _vault()
    vault.set_status = AsyncMock(side_effect=NoteNotFound("Не нашёл заметку «X» в хранилище"))
    cb, _, _ = await _press(db, "read", nid, vault=vault)
    assert db.get_note(nid).read is False
    assert "Не нашёл заметку" in sent_texts(cb.message.reply)[-1]
    cb.message.edit_reply_markup.assert_not_awaited()


async def test_b2_read_push_failed_still_marked(db):
    nid = _note(db)
    vault = _vault()
    vault.set_status = AsyncMock(side_effect=VaultError("не отправлено в GitHub", note_name=NAME))
    cb, _, _ = await _press(db, "read", nid, vault=vault)
    assert db.get_note(nid).read is True
    assert "не отправлено" in sent_texts(cb.message.reply)[-1]


async def test_b3_delete_asks_confirmation(db):
    nid = _note(db)
    cb, vault, _ = await _press(db, "del", nid)
    vault.delete.assert_not_awaited()
    assert [(t, a) for t, a, _ in buttons(_markup(cb.message.edit_reply_markup))] == [
        ("✅ Да, удалить", "del_yes"),
        ("↩️ Нет", "del_no"),
    ]


async def test_b3_delete_no_restores_buttons(db):
    nid = _note(db)
    db.mark_detailed(nid)
    cb, vault, _ = await _press(db, "del_no", nid)
    vault.delete.assert_not_awaited()
    assert db.get_note(nid) is not None
    assert [a for _, a, _ in buttons(_markup(cb.message.edit_reply_markup))] == ["read", "del"]


async def test_b3_delete_yes(db):
    nid = _note(db)
    cb, vault, _ = await _press(db, "del_yes", nid)
    vault.delete.assert_awaited_once_with(NAME, URL, skip=set())
    assert db.get_note(nid) is None
    assert db.find_digest(["youtube:abc"]) is None  # ссылку можно прислать снова
    call = cb.message.edit_text.call_args
    assert call.args[0] == f"🗑 Удалено: «{NAME}»"
    assert call.kwargs.get("reply_markup") is None


async def test_b6_delete_missing_file_cleans_db(db):
    nid = _note(db)
    vault = _vault()
    vault.delete = AsyncMock(side_effect=NoteNotFound("нет"))
    cb, _, _ = await _press(db, "del_yes", nid, vault=vault)
    assert db.get_note(nid) is None
    assert "уже нет" in cb.message.edit_text.call_args.args[0]


async def test_b4_more_queues_detailed_job(db):
    nid = _note(db)
    cb, _, queue = await _press(db, "more", nid)
    job: Job = queue.get_nowait()
    assert job.url == URL and job.force is True and job.note_id == nid
    assert job.hint == DETAILED_HINT and "подробн" in DETAILED_HINT
    assert job.status_msg_id == 777 and job.chat_id == OWNER
    [row] = db.pending_jobs()
    assert row.id == job.id and row.note_id == nid
    assert db.get_note(nid).detailed is True
    assert [a for _, a, _ in buttons(_markup(cb.message.edit_reply_markup))] == ["read", "del"]


async def test_b4_more_twice_no_second_job(db):
    nid = _note(db)
    queue: asyncio.Queue[Job] = asyncio.Queue()
    await _press(db, "more", nid, queue=queue)
    cb, _, _ = await _press(db, "more", nid, queue=queue)
    assert queue.qsize() == 1
    assert len(db.pending_jobs()) == 1
    text = cb.answer.call_args.args[0]
    assert "уже" in text


async def test_b7_unknown_note(db):
    cb, vault, queue = await _press(db, "read", 12345)
    assert "не найдена" in cb.answer.call_args.args[0]
    vault.set_status.assert_not_awaited()
    assert queue.empty()


async def test_no_vault(db):
    nid = _note(db)
    cb = _cb()
    await on_note(
        cb,
        callback_data=NoteCb(action="read", id=nid),
        db=db,
        vault=None,
        link_queue=asyncio.Queue(),
    )
    assert "VAULT_REPO" in cb.answer.call_args.args[0]


async def test_d2_unread_command():
    vault = _vault()
    vault.unread = AsyncMock(return_value=[UnreadNote("n", "⌨️ Старая", date(2026, 11, 1), 5)])
    msg = make_message("/unread")
    await cmd_unread(msg, vault=vault)
    text = sent_texts(msg.answer)[-1]
    assert "Непрочитанных конспектов: 1" in text and "Старая" in text


async def test_d2_unread_command_all_read():
    msg = make_message("/unread")
    await cmd_unread(msg, vault=_vault())
    assert sent_texts(msg.answer)[-1] == "Всё прочитано 🎉"


async def test_r1_delete_passes_other_names(db):
    nid = _note(db)
    db.add_note("2026-11-09 Хоткеи (2)", URL, "youtube", OWNER)
    _, vault, _ = await _press(db, "del_yes", nid)
    vault.delete.assert_awaited_once_with(NAME, URL, skip={"2026-11-09 Хоткеи (2)"})


async def test_r4_more_failure_resets_flag(db):
    nid = _note(db)
    cb = _cb()
    cb.message.reply = AsyncMock(side_effect=RuntimeError("telegram"))
    try:
        await on_note(
            cb, callback_data=NoteCb(action="more", id=nid), db=db, vault=_vault(),
            link_queue=asyncio.Queue(),
        )  # fmt: skip
    except RuntimeError:
        pass
    assert db.get_note(nid).detailed is False
