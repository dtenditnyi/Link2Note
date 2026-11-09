"""Этап 3 в воркере: кнопки под конспектом, «Подробнее» заменяет заметку, note_id в очереди."""

import asyncio
from unittest.mock import AsyncMock

from digest.errors import DigestError, GeminiBusy, VaultError
from digest.worker import Job, process_job, restore_jobs
from handlers.keyboards import NoteCb
from tests.conftest import OWNER
from tests.test_worker import CANON, JOB, VID, FakePostponer, _bot, _deps, _http, _llm, _vault


def buttons(markup) -> list[tuple[str, str, int]]:
    """[(текст, action, id)] по всем кнопкам клавиатуры."""
    out = []
    for row in markup.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
            cb = NoteCb.unpack(b.callback_data)
            out.append((b.text, cb.action, cb.id))
    return out


def last_markup(bot):
    return bot.edit_message_text.call_args.kwargs.get("reply_markup")


def _texts(bot):
    return [c.kwargs["text"] for c in bot.edit_message_text.call_args_list]


async def test_b1_success_has_buttons_and_note_row(db):
    deps = _deps(db)
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    note = db.find_note([f"youtube:{VID}"])
    assert note is not None
    assert note.note_name == "2026-11-09 Хоткеи"
    assert note.url == CANON  # url из заметки, а не сырой youtu.be?si=
    assert note.chat_id == 1
    assert buttons(last_markup(deps["bot"])) == [
        ("🔁 Подробнее", "more", note.id),
        ("✅ Прочитано", "read", note.id),
        ("🗑 Удалить", "del", note.id),
    ]


async def test_b1_vault_error_with_note_still_gets_row(db):
    deps = _deps(db, vault=_vault(exc=VaultError("не отправлена", note_name="2026-11-09 X")))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert db.find_note([f"youtube:{VID}"]).note_name == "2026-11-09 X"


async def test_b7_duplicate_has_buttons(db):
    nid = db.add_note("2026-11-01 Старая", CANON, "youtube", OWNER)
    db.save_digest([f"youtube:{VID}"], "2026-11-01 Старая", CANON, "youtube", note_id=nid)
    deps = _deps(db)
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert _texts(deps["bot"])[-1] == "♻️ Уже есть: «2026-11-01 Старая»"
    assert [a for _, a, _ in buttons(last_markup(deps["bot"]))] == ["more", "read", "del"]


async def test_b7_old_duplicate_without_note_gets_buttons(db):
    db.save_digest([f"youtube:{VID}"], "2026-11-01 Старая", CANON, "youtube")
    deps = _deps(db)
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert last_markup(deps["bot"]) is not None


async def test_errors_have_no_buttons(db):
    deps = _deps(db, llm=_llm(exc=DigestError("плохо")))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert last_markup(deps["bot"]) is None


def _detailed_job(db, nid, msg=20):
    jid = db.add_job(CANON, "подробно", True, 1, msg, note_id=nid)
    return Job(url=CANON, hint="подробно", chat_id=1, status_msg_id=msg, force=True, id=jid,
               note_id=nid)  # fmt: skip


async def test_b4_detailed_replaces_same_note(db):
    nid = db.add_note("2026-11-01 Старая", CANON, "youtube", OWNER)
    db.save_digest([f"youtube:{VID}"], "2026-11-01 Старая", CANON, "youtube", note_id=nid)
    db.mark_detailed(nid)
    vault = _vault()
    vault.replace = AsyncMock(return_value="2026-11-01 Старая")
    deps = _deps(db, vault=vault)
    async with _http() as http:
        await process_job(_detailed_job(db, nid), http=http, **deps)

    deps["llm"].digest_youtube.assert_awaited_once_with(CANON, "подробно")  # дубликат не мешает
    vault.save.assert_not_awaited()
    name, url, md = vault.replace.await_args.args
    assert (name, url) == ("2026-11-01 Старая", CANON)
    assert "status: unread" in md
    assert _texts(deps["bot"])[-1].startswith("🔁 Подробный конспект готов")
    assert [a for _, a, _ in buttons(last_markup(deps["bot"]))] == ["read", "del"]
    assert db.pending_jobs() == []


async def test_b6_detailed_renamed_note_updates_db(db):
    nid = db.add_note("Старое имя", CANON, "youtube", OWNER)
    vault = _vault()
    vault.replace = AsyncMock(return_value="Новое имя")
    async with _http() as http:
        await process_job(_detailed_job(db, nid), http=http, **_deps(db, vault=vault))
    assert db.get_note(nid).note_name == "Новое имя"


async def test_b4_detailed_failure_allows_retry(db):
    nid = db.add_note("N", CANON, "youtube", OWNER)
    db.mark_detailed(nid)
    deps = _deps(db, llm=_llm(exc=DigestError("плохо")))
    async with _http() as http:
        await process_job(_detailed_job(db, nid), http=http, **deps)
    assert db.get_note(nid).detailed is False


async def test_b4_detailed_for_deleted_note_skips_gemini(db):
    nid = db.add_note("N", CANON, "youtube", OWNER)
    job = _detailed_job(db, nid)
    db.delete_note(nid)
    deps = _deps(db)
    async with _http() as http:
        await process_job(job, http=http, **deps)
    deps["llm"].digest_youtube.assert_not_awaited()
    assert "удалена" in _texts(deps["bot"])[-1]


async def test_b5_detailed_postponed_keeps_flag(db):
    nid = db.add_note("N", CANON, "youtube", OWNER)
    db.mark_detailed(nid)
    postponer = FakePostponer()
    deps = _deps(db, llm=_llm(exc=GeminiBusy("перегружен")), postponer=postponer)
    async with _http() as http:
        await process_job(_detailed_job(db, nid), http=http, **deps)
    [(job, _)] = postponer.calls
    assert job.note_id == nid
    assert db.get_note(nid).detailed is True  # кнопка не возвращается, задача ещё в работе


async def test_b5_restore_keeps_note_id(db):
    nid = db.add_note("N", CANON, "youtube", OWNER)
    _detailed_job(db, nid)
    queue: asyncio.Queue[Job] = asyncio.Queue()
    assert await restore_jobs(db, queue, _bot()) == 1
    assert queue.get_nowait().note_id == nid


async def test_whisper_deps_passed_to_pipeline(db, monkeypatch):
    from digest import worker

    seen = {}

    async def fake_make_note(url, hint, **kw):
        seen.update(kw)
        raise DigestError("стоп")

    monkeypatch.setattr(worker, "make_note", fake_make_note)
    tr = object()
    async with _http() as http:
        await process_job(JOB, http=http, **_deps(db), transcriber=tr, whisper_max_seconds=900)
    assert seen["transcriber"] is tr and seen["whisper_max_seconds"] == 900


async def test_r2_detailed_resets_read(db):
    nid = db.add_note("N", CANON, "youtube", OWNER)
    db.mark_read(nid)
    vault = _vault()
    vault.replace = AsyncMock(return_value="N")
    deps = _deps(db, vault=vault)
    async with _http() as http:
        await process_job(_detailed_job(db, nid), http=http, **deps)
    assert db.get_note(nid).read is False  # файл снова unread
    assert [a for _, a, _ in buttons(last_markup(deps["bot"]))] == ["read", "del"]


async def test_r3_detailed_push_failed_is_done(db):
    nid = db.add_note("N", CANON, "youtube", OWNER)
    db.mark_detailed(nid)
    vault = _vault()
    vault.replace = AsyncMock(side_effect=VaultError("не отправлена в GitHub", note_name="N"))
    deps = _deps(db, vault=vault)
    async with _http() as http:
        await process_job(_detailed_job(db, nid), http=http, **deps)
    assert db.get_note(nid).detailed is True  # квоту второй раз не тратим
    text = _texts(deps["bot"])[-1]
    assert text.startswith("🔁 Подробный конспект готов") and "⚠️" in text
    assert last_markup(deps["bot"]) is not None


async def test_r5_legacy_duplicate_gets_canonical_url(db):
    db.save_digest([f"youtube:{VID}"], "Old", JOB.url, "youtube")
    async with _http() as http:
        await process_job(JOB, http=http, **_deps(db))
    assert db.find_note([f"youtube:{VID}"]).url == CANON
