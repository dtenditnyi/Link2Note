import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from digest import pipeline
from digest.errors import DigestError, GeminiBusy, VaultError
from digest.fetchers import Article, VideoMeta, VideoOptions
from digest.llm import Digested
from digest.pipeline import make_note
from digest.schema import Digest
from digest.worker import Job, Postponer, process_job, restore_jobs, run_worker
from tests.conftest import OWNER, TZ

VID = "dQw4w9WgXcQ"
CANON = f"https://www.youtube.com/watch?v={VID}"
DAY = date(2026, 11, 9)
REEL = "https://www.instagram.com/reel/C1abc/?igsh=x"
SHORT_TT = "https://vm.tiktok.com/ZMabc/"
ARTICLE = "https://blog.example.com/post"


def _digest(**kw) -> Digest:
    data = {
        "title": "Хоткеи <VS Code> & co",
        "emoji": "⌨️",
        "tldr": "Суть <b>жирно</b>",
        "key_points": ["a"],
        "tags": ["dev"],
        "read_minutes": 2,
        "confidence": "high",
    }
    data.update(kw)
    return Digest.model_validate(data)


def _http(status=200, payload=None):
    payload = payload or {"title": "t", "author_name": "Fireship"}
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload))
    )


def _llm(result=None, exc=None, model="gemini-test"):
    llm = MagicMock()
    out = Digested(result or _digest(), model)
    for name in ("digest_youtube", "digest_video_file", "digest_text"):
        setattr(llm, name, AsyncMock(return_value=out, side_effect=exc))
    return llm


def _vault(name="2026-11-09 Хоткеи", exc=None):
    v = MagicMock()
    v.save = AsyncMock(return_value=name, side_effect=exc)
    return v


def _bot():
    b = MagicMock()
    b.edit_message_text = AsyncMock()
    b.send_message = AsyncMock()
    return b


NOW = datetime(2026, 11, 9, 14, 0, tzinfo=TZ)  # 09.11.2026 14:00 по Бухаресту


class FakePostponer:
    """Запоминает отложенные задачи вместо настоящих таймеров."""

    def __init__(self):
        self.calls: list[tuple[Job, datetime]] = []

    def later(self, job, at):
        self.calls.append((job, at))


def _texts(bot) -> list[str]:
    return [c.kwargs["text"] for c in bot.edit_message_text.call_args_list]


JOB = Job(url=f"https://youtu.be/{VID}?si=x", hint="упор на хоткеи", chat_id=1, status_msg_id=10)


@pytest.fixture
def fake_download(monkeypatch):
    """download_video без сети: кладёт файл в out_dir и запоминает папку."""
    seen: dict = {}

    async def download(url, out_dir: Path, opts, **kw):
        seen["dir"] = out_dir
        seen["opts"] = opts
        path = out_dir / "C1abc.mp4"
        path.write_bytes(b"video")
        if seen.get("exc"):
            raise seen["exc"]
        return path, seen.get(
            "meta", VideoMeta(title="Reel", author="some.user", duration=42, key="instagram:C1abc")
        )

    monkeypatch.setattr(pipeline, "download_video", download)
    return seen


@pytest.fixture
def fake_article(monkeypatch):
    seen: dict = {}

    async def fetch(url, http):
        seen["url"] = url
        return Article(title="Советы", author="", sitename="Example Blog", text="текст " * 100)

    monkeypatch.setattr(pipeline, "fetch_article", fetch)
    return seen


# --- pipeline ------------------------------------------------------------------------


async def test_pipeline_uses_canonical_url_and_author():
    llm = _llm()
    async with _http() as http:
        note = await make_note(JOB.url, JOB.hint, llm=llm, http=http, today=DAY)
    llm.digest_youtube.assert_awaited_once_with(CANON, "упор на хоткеи")
    assert 'author: "Fireship"' in note.markdown
    assert f'url: "{CANON}"' in note.markdown
    assert "date_added: 2026-11-09" in note.markdown
    assert note.digest.title.startswith("Хоткеи")
    assert note.keys == [f"youtube:{VID}"]


async def test_pipeline_rejects_non_video():
    async with _http() as http:
        with pytest.raises(DigestError):
            await make_note(
                "https://www.youtube.com/@channel", "", llm=_llm(), http=http, today=DAY
            )


async def test_pipeline_private_video_skips_gemini():
    llm = _llm()
    async with _http(status=401) as http:
        with pytest.raises(DigestError):
            await make_note(JOB.url, "", llm=llm, http=http, today=DAY)
    llm.digest_youtube.assert_not_awaited()


async def test_p1_reel_goes_through_download_and_files_api(fake_download):
    llm = _llm()
    opts = VideoOptions(max_seconds=60)
    async with _http() as http:
        note = await make_note(REEL, "кратко", llm=llm, http=http, today=DAY, video=opts)
    path, mime, hint = llm.digest_video_file.call_args.args
    assert Path(path).name == "C1abc.mp4"
    assert mime == "video/mp4"
    assert hint == "кратко"
    llm.digest_youtube.assert_not_awaited()
    assert fake_download["opts"] is opts
    assert not fake_download["dir"].exists()  # временная папка удалена
    assert "source: instagram" in note.markdown
    assert 'author: "some.user"' in note.markdown
    assert 'duration: "00:42"' in note.markdown
    assert f'url: "{REEL}"' in note.markdown
    assert note.keys == ["instagram:C1abc"]  # normalize и ключ yt-dlp совпали — без повтора


async def test_p1_temp_dir_removed_on_error(fake_download):
    fake_download["exc"] = DigestError("не смог скачать")
    llm = _llm()
    async with _http() as http:
        with pytest.raises(DigestError):
            await make_note(REEL, "", llm=llm, http=http, today=DAY)
    assert not fake_download["dir"].exists()
    llm.digest_video_file.assert_not_awaited()


async def test_p1_short_tiktok_gets_both_keys(fake_download):
    fake_download["meta"] = VideoMeta(title="t", author="u", duration=10, key="tiktok:7301")
    async with _http() as http:
        note = await make_note(SHORT_TT, "", llm=_llm(), http=http, today=DAY)
    assert note.keys == ["vm.tiktok.com/ZMabc", "tiktok:7301"]
    assert "source: tiktok" in note.markdown


async def test_p1_article_goes_through_text(fake_article):
    llm = _llm()
    async with _http() as http:
        note = await make_note(ARTICLE, "упор на выводы", llm=llm, http=http, today=DAY)
    assert fake_article["url"] == ARTICLE
    (text,) = llm.digest_text.call_args.args
    assert text.startswith("текст")
    assert llm.digest_text.call_args.kwargs == {"title": "Советы", "hint": "упор на выводы"}
    assert "source: article" in note.markdown
    assert 'author: "Example Blog"' in note.markdown  # автора нет — пишем сайт
    assert note.keys == ["blog.example.com/post"]


async def test_p1_known_url_stops_before_any_work(fake_download):
    llm = _llm()
    async with _http() as http:
        with pytest.raises(pipeline.DuplicateError) as exc:
            await make_note(
                REEL,
                "",
                llm=llm,
                http=http,
                today=DAY,
                known=lambda keys: "старая" if "instagram:C1abc" in keys else None,
            )
    assert exc.value.note_name == "старая"
    assert "dir" not in fake_download
    llm.digest_video_file.assert_not_awaited()


async def test_p1_known_ytdlp_key_stops_before_gemini(fake_download):
    fake_download["meta"] = VideoMeta(title="t", author="u", duration=10, key="tiktok:7301")
    llm = _llm()
    async with _http() as http:
        with pytest.raises(pipeline.DuplicateError):
            await make_note(
                SHORT_TT,
                "",
                llm=llm,
                http=http,
                today=DAY,
                known=lambda keys: "старая" if "tiktok:7301" in keys else None,
            )
    llm.digest_video_file.assert_not_awaited()
    assert not fake_download["dir"].exists()


async def test_p2_step_timings_logged(fake_download, caplog):
    caplog.set_level(logging.INFO)
    async with _http() as http:
        await make_note(REEL, "", llm=_llm(), http=http, today=DAY)
    timings = [r.getMessage() for r in caplog.records if "⏱" in r.getMessage()]
    assert any("download" in m for m in timings)
    assert any("gemini" in m for m in timings)


async def test_progress_messages_by_source(fake_download, fake_article):
    for url, expected in (
        (JOB.url, ["🧠 Смотрю видео…"]),
        (REEL, ["⬇️ Скачиваю видео…", "🧠 Смотрю видео…"]),
        (ARTICLE, ["📖 Читаю статью…", "🧠 Делаю конспект…"]),
    ):
        seen: list[str] = []

        async def progress(text, seen=seen):
            seen.append(text)

        async with _http() as http:
            await make_note(url, "", llm=_llm(), http=http, today=DAY, progress=progress)
        assert seen == expected, url


# --- worker --------------------------------------------------------------------------


def _deps(db, **kw):
    deps = {
        "bot": _bot(),
        "llm": _llm(),
        "vault": _vault(),
        "today": lambda: DAY,
        "db": db,
        "clock": lambda: NOW,
        "postponer": FakePostponer(),
    }
    deps.update(kw)
    return deps


async def test_w1_success_message_escaped(db):
    deps = _deps(db)
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    bot, vault = deps["bot"], deps["vault"]
    texts = _texts(bot)
    assert texts[0] == "🧠 Смотрю видео…"
    assert texts[-1] == (
        "⌨️ <b>Хоткеи &lt;VS Code&gt; &amp; co</b>\n\n"
        "Суть &lt;b&gt;жирно&lt;/b&gt;\n\n"
        "📝 Заметка: «2026-11-09 Хоткеи»"
    )
    call = bot.edit_message_text.call_args
    assert call.kwargs["chat_id"] == 1 and call.kwargs["message_id"] == 10
    title, md, day = vault.save.call_args.args
    assert title == "Хоткеи <VS Code> & co" and day == DAY
    assert md.startswith("---\ntype: digest")


async def test_w2_digest_error_shown(db):
    deps = _deps(db, llm=_llm(exc=DigestError("ИИ вернул <пусто>")))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert _texts(deps["bot"])[-1] == "❌ Не получилось: ИИ вернул &lt;пусто&gt;"


async def test_w2_vault_error_shown(db):
    err = VaultError("Заметка «x» сохранена на сервере, но не отправлена в GitHub", note_name="x")
    deps = _deps(db, vault=_vault(exc=err))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    last = _texts(deps["bot"])[-1]
    assert "не отправлена в GitHub" in last
    assert last.startswith("❌")


async def test_w2_unexpected_error_hidden(db, caplog):
    deps = _deps(db, llm=_llm(exc=RuntimeError("secret path /app/x")))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    last = _texts(deps["bot"])[-1]
    assert "внутренняя ошибка" in last
    assert "secret" not in last
    assert "secret path" in caplog.text


async def test_w3_worker_survives_edit_failure_and_keeps_order(db):
    bot = _bot()
    calls: list[int] = []

    async def edit(**kw):
        calls.append(kw["message_id"])
        if kw["message_id"] == 1:
            raise RuntimeError("message to edit not found")

    bot.edit_message_text = AsyncMock(side_effect=edit)
    queue: asyncio.Queue[Job] = asyncio.Queue()
    # Разные видео: одно и то же второй раз было бы дубликатом.
    for i, vid in ((1, VID), (2, "aaaaaaaaaaa")):
        queue.put_nowait(Job(url=f"https://youtu.be/{vid}", hint="", chat_id=1, status_msg_id=i))

    async with _http() as http:
        task = asyncio.create_task(run_worker(queue, http=http, **_deps(db, bot=bot)))
        await asyncio.wait_for(queue.join(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert 2 in calls
    assert calls.index(2) > max(i for i, m in enumerate(calls) if m == 1)  # W4: по порядку


# --- Этап 2: дубликаты и очередь в SQLite -------------------------------------------------


async def test_w5_duplicate_skips_everything(db):
    db.save_digest([f"youtube:{VID}"], "2026-11-01 Старая", CANON, "youtube")
    deps = _deps(db)
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert _texts(deps["bot"])[-1] == "♻️ Уже есть: «2026-11-01 Старая»"
    deps["llm"].digest_youtube.assert_not_awaited()
    deps["vault"].save.assert_not_awaited()


async def test_w5_force_makes_new_note(db):
    db.save_digest([f"youtube:{VID}"], "2026-11-01 Старая", CANON, "youtube")
    deps = _deps(db, vault=_vault(name="2026-11-09 Новая"))
    job = Job(url=JOB.url, hint="", chat_id=1, status_msg_id=10, force=True)
    async with _http() as http:
        await process_job(job, http=http, **deps)
    deps["llm"].digest_youtube.assert_awaited_once()
    assert db.find_digest([f"youtube:{VID}"]) == "2026-11-09 Новая"


async def test_w6_all_keys_recorded(db, fake_download):
    fake_download["meta"] = VideoMeta(title="t", author="u", duration=10, key="tiktok:7301")
    deps = _deps(db, vault=_vault(name="2026-11-09 Ролик"))
    job = Job(url=SHORT_TT, hint="", chat_id=1, status_msg_id=10)
    async with _http() as http:
        await process_job(job, http=http, **deps)
    assert db.find_digest(["vm.tiktok.com/ZMabc"]) == "2026-11-09 Ролик"
    assert db.find_digest(["tiktok:7301"]) == "2026-11-09 Ролик"


async def test_w6_vault_error_with_note_still_recorded(db):
    err = VaultError("не отправлена в GitHub", note_name="2026-11-09 Локальная")
    deps = _deps(db, vault=_vault(exc=err))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert db.find_digest([f"youtube:{VID}"]) == "2026-11-09 Локальная"


async def test_w6_failure_not_recorded(db):
    deps = _deps(db, llm=_llm(exc=DigestError("нет")))
    async with _http() as http:
        await process_job(JOB, http=http, **deps)
    assert db.find_digest([f"youtube:{VID}"]) is None


@pytest.mark.parametrize(
    ("kw", "status"),
    [
        ({}, "done"),
        ({"llm": "digest_error"}, "failed"),
        ({"llm": "unexpected"}, "failed"),
    ],
)
async def test_w7_job_finished_in_db(db, kw, status):
    jid = db.add_job(JOB.url, "", False, OWNER, 10)
    job = Job(url=JOB.url, hint="", chat_id=OWNER, status_msg_id=10, id=jid)
    llm = {
        "digest_error": _llm(exc=DigestError("нет")),
        "unexpected": _llm(exc=RuntimeError("boom")),
    }.get(kw.get("llm", ""), _llm())
    async with _http() as http:
        await process_job(job, http=http, **_deps(db, llm=llm))
    assert db.pending_jobs() == []
    with db._conn() as c:
        row = c.execute("SELECT status, attempts FROM link_jobs WHERE id = ?", (jid,)).fetchone()
    assert row == (status, 1)


async def test_w7_duplicate_job_is_done(db):
    db.save_digest([f"youtube:{VID}"], "x", CANON, "youtube")
    jid = db.add_job(JOB.url, "", False, OWNER, 10)
    job = Job(url=JOB.url, hint="", chat_id=OWNER, status_msg_id=10, id=jid)
    async with _http() as http:
        await process_job(job, http=http, **_deps(db))
    assert db.pending_jobs() == []


async def test_w8_restore_jobs(db):
    fresh = db.add_job("https://a", "упор", True, OWNER, 11)
    crashing = db.add_job("https://b", "", False, OWNER, 12)
    for _ in range(3):
        db.start_job(crashing)
    db.start_job(fresh)  # прерван один раз — ещё можно повторить
    bot = _bot()
    queue: asyncio.Queue[Job] = asyncio.Queue()

    restored = await restore_jobs(db, queue, bot)

    assert restored == 1
    job = queue.get_nowait()
    assert job == Job(
        url="https://a", hint="упор", chat_id=OWNER, status_msg_id=11, force=True, id=fresh
    )
    assert queue.empty()
    texts = {c.kwargs["message_id"]: c.kwargs["text"] for c in bot.edit_message_text.call_args_list}
    assert texts[11] == "⏳ После перезапуска снова в очереди…"
    assert texts[12].startswith("❌") and "3 раза" in texts[12]
    assert [j.id for j in db.pending_jobs()] == [fresh]


async def test_w8_restore_survives_edit_failure(db):
    db.add_job("https://a", "", False, OWNER, 11)
    bot = _bot()
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError("message not found"))
    queue: asyncio.Queue[Job] = asyncio.Queue()
    assert await restore_jobs(db, queue, bot) == 1
    assert queue.qsize() == 1


async def test_w9_cancelled_job_stays_pending_without_attempt(db):
    """Обычная остановка бота посреди задачи — не падение: задача вернётся после старта."""
    jid = db.add_job(JOB.url, "", False, OWNER, 10)
    job = Job(url=JOB.url, hint="", chat_id=OWNER, status_msg_id=10, id=jid)
    deps = _deps(db, llm=_llm(exc=asyncio.CancelledError()))
    async with _http() as http:
        with pytest.raises(asyncio.CancelledError):
            await process_job(job, http=http, **deps)
    [pending] = db.pending_jobs()
    assert pending.id == jid
    assert pending.attempts == 0
    assert not any(t.startswith("❌") for t in _texts(deps["bot"]))


# --- Модель в заметке (M2) --------------------------------------------------------------------


async def test_m2_note_has_model(caplog):
    caplog.set_level(logging.INFO)
    async with _http() as http:
        note = await make_note(
            JOB.url, "", llm=_llm(model="gemini-3.7-flash"), http=http, today=DAY
        )
    assert note.model == "gemini-3.7-flash"
    assert "\nmodel: gemini-3.7-flash\n" in note.markdown
    assert "gemini-3.7-flash" in caplog.text


# --- Gemini перегружен: откладывание (Q1–Q6) -----------------------------------------------


def _busy_job(db, *, defers=0, quota_waits=0):
    jid = db.add_job(JOB.url, "", False, OWNER, 10)
    return Job(
        url=JOB.url,
        hint="",
        chat_id=OWNER,
        status_msg_id=10,
        id=jid,
        defers=defers,
        quota_waits=quota_waits,
    )


@pytest.mark.parametrize(
    ("defers", "minutes", "hhmm"), [(0, 5, "14:05"), (1, 15, "14:15"), (2, 30, "14:30")]
)
async def test_q1_busy_postponed(db, defers, minutes, hhmm):
    job = _busy_job(db, defers=defers)
    deps = _deps(db, llm=_llm(exc=GeminiBusy("перегружен", daily=False)))
    async with _http() as http:
        await process_job(job, http=http, **deps)

    last = _texts(deps["bot"])[-1]
    assert last == f"⏳ Gemini перегружен, попробую снова в {hhmm} (повтор {defers + 1} из 3)"
    [(queued, at)] = deps["postponer"].calls
    assert at == NOW + timedelta(minutes=minutes)
    assert queued.id == job.id and queued.defers == defers + 1
    [pending] = db.pending_jobs()
    assert pending.run_after == at and pending.defers == 1  # в базе +1 к сохранённому
    assert pending.attempts == 0  # откладывание — не падение


async def test_q2_busy_after_three_defers_fails(db):
    job = _busy_job(db, defers=3)
    deps = _deps(db, llm=_llm(exc=GeminiBusy("перегружен", daily=False)))
    async with _http() as http:
        await process_job(job, http=http, **deps)
    assert _texts(deps["bot"])[-1].startswith("❌ Не получилось: Gemini перегружен уже ~50 минут")
    assert deps["postponer"].calls == []
    assert db.pending_jobs() == []


async def test_q3_daily_quota_waits_until_reset(db):
    job = _busy_job(db, defers=2)
    deps = _deps(db, llm=_llm(exc=GeminiBusy("лимит", daily=True)))
    async with _http() as http:
        await process_job(job, http=http, **deps)
    [(queued, at)] = deps["postponer"].calls
    # Полночь по тихоокеанскому времени 10.11 = 10:00 по Бухаресту, +5 минут.
    assert at == datetime(2026, 11, 10, 10, 5, tzinfo=TZ)
    assert queued.defers == 2 and queued.quota_waits == 1  # счётчик перегрузки не тронут
    assert _texts(deps["bot"])[-1] == "⏳ Лимит Gemini на сегодня исчерпан — попробую снова в 10:05"
    [pending] = db.pending_jobs()
    assert (pending.defers, pending.quota_waits) == (0, 1)


async def test_q3_daily_quota_second_time_fails(db):
    job = _busy_job(db, quota_waits=1)
    deps = _deps(db, llm=_llm(exc=GeminiBusy("лимит", daily=True)))
    async with _http() as http:
        await process_job(job, http=http, **deps)
    assert _texts(deps["bot"])[-1].startswith("❌ Не получилось: лимит Gemini на сегодня исчерпан")
    assert deps["postponer"].calls == []
    assert db.pending_jobs() == []


async def test_q4_postponed_job_does_not_block_queue(db):
    llm = _llm()
    llm.digest_youtube = AsyncMock(
        side_effect=[GeminiBusy("перегружен", daily=False), Digested(_digest(), "gemini-test")]
    )
    deps = _deps(db, llm=llm)
    queue: asyncio.Queue[Job] = asyncio.Queue()
    queue.put_nowait(_busy_job(db))
    queue.put_nowait(Job(url="https://youtu.be/aaaaaaaaaaa", hint="", chat_id=1, status_msg_id=11))
    async with _http() as http:
        task = asyncio.create_task(run_worker(queue, http=http, **deps))
        await asyncio.wait_for(queue.join(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(deps["postponer"].calls) == 1
    deps["vault"].save.assert_awaited_once()  # вторая ссылка обработана сразу


async def test_q5_postponer_requeues_and_close_cancels():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    postponer = Postponer(queue)
    soon = Job(url="https://a", hint="", chat_id=1, status_msg_id=1)
    never = Job(url="https://b", hint="", chat_id=1, status_msg_id=2)
    postponer.later(soon, datetime.now(UTC) + timedelta(milliseconds=50))
    postponer.later(never, datetime.now(UTC) + timedelta(hours=1))
    assert await asyncio.wait_for(queue.get(), timeout=2) == soon
    postponer.close()
    await asyncio.sleep(0)
    assert queue.empty()


async def test_q5_postponer_past_time_requeues_immediately():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    postponer = Postponer(queue)
    job = Job(url="https://a", hint="", chat_id=1, status_msg_id=1)
    postponer.later(job, datetime.now(UTC) - timedelta(minutes=5))
    assert await asyncio.wait_for(queue.get(), timeout=1) == job
    postponer.close()


async def test_q6_postponed_job_succeeds_later(db):
    job = _busy_job(db)
    deps = _deps(db, llm=_llm(exc=GeminiBusy("перегружен", daily=False)))
    async with _http() as http:
        await process_job(job, http=http, **deps)
        [(again, _)] = deps["postponer"].calls
        deps["llm"] = _llm(model="gemini-3.7-flash")
        await process_job(again, http=http, **deps)
    assert db.pending_jobs() == []
    assert db.find_digest([f"youtube:{VID}"]) == "2026-11-09 Хоткеи"
    assert _texts(deps["bot"])[-1].startswith("⌨️ <b>")


# --- Перезапуск (R1, R2) ----------------------------------------------------------------


async def test_r1_restore_respects_run_after(db):
    now_utc = datetime.now(UTC)
    later_id = db.add_job("https://later", "", False, OWNER, 21)
    db.postpone_job(later_id, now_utc + timedelta(minutes=10), daily=False)
    past_id = db.add_job("https://past", "", False, OWNER, 22)
    db.postpone_job(past_id, now_utc - timedelta(minutes=1), daily=False)
    plain_id = db.add_job("https://plain", "", False, OWNER, 23)

    bot, queue, postponer = _bot(), asyncio.Queue(), FakePostponer()
    restored = await restore_jobs(
        db, queue, bot, postponer=postponer, clock=lambda: now_utc.astimezone(TZ)
    )

    assert restored == 3
    assert sorted(queue.get_nowait().id for _ in range(2)) == sorted([past_id, plain_id])
    [(job, at)] = postponer.calls
    assert job.id == later_id and job.defers == 1
    assert abs(at - (now_utc + timedelta(minutes=10))) < timedelta(seconds=1)
    edited = {c.kwargs["message_id"] for c in bot.edit_message_text.call_args_list}
    assert 21 not in edited  # статус с временем повтора не перезаписан


async def test_r2_restart_summary_is_new_message(db):
    now = NOW
    later_id = db.add_job("https://later", "", False, OWNER, 21)
    db.postpone_job(later_id, now + timedelta(minutes=25), daily=False)
    db.add_job("https://plain", "", False, OWNER, 23)
    bot = _bot()
    await restore_jobs(db, asyncio.Queue(), bot, postponer=FakePostponer(), clock=lambda: now)
    [call] = bot.send_message.call_args_list
    chat_id = call.kwargs.get("chat_id", call.args[0] if call.args else None)
    text = call.kwargs.get("text", call.args[1] if len(call.args) > 1 else "")
    assert chat_id == OWNER
    assert text.startswith("🔄 Бот перезапущен — вернул в очередь ссылок: 2")
    assert "ждут Gemini: 1" in text and "14:25" in text


async def test_r2_no_jobs_no_message(db):
    bot = _bot()
    assert await restore_jobs(db, asyncio.Queue(), bot, postponer=FakePostponer()) == 0
    bot.send_message.assert_not_called()


async def test_r2_summary_send_failure_is_not_fatal(db):
    db.add_job("https://plain", "", False, OWNER, 23)
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))
    queue: asyncio.Queue[Job] = asyncio.Queue()
    assert await restore_jobs(db, queue, bot, postponer=FakePostponer()) == 1
    assert queue.qsize() == 1


# --- После ревью ------------------------------------------------------------------------


async def test_q7_cancel_during_postpone_message_keeps_job_pending(db):
    """Остановка бота ровно во время правки «⏳ Gemini перегружен…» не теряет задачу."""
    job = _busy_job(db)
    bot = _bot()
    calls = {"n": 0}

    async def edit(**kw):
        calls["n"] += 1
        if kw["text"].startswith("⏳ Gemini перегружен"):
            raise asyncio.CancelledError

    bot.edit_message_text = AsyncMock(side_effect=edit)
    deps = _deps(db, bot=bot, llm=_llm(exc=GeminiBusy("перегружен", daily=False)))
    async with _http() as http:
        with pytest.raises(asyncio.CancelledError):
            await process_job(job, http=http, **deps)
    [pending] = db.pending_jobs()
    assert pending.id == job.id
    assert pending.run_after == NOW + timedelta(minutes=5)
    assert pending.attempts == 0


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 00:02 PST 10.11 = 10:02 по Бухаресту: сброс только что был → сегодня 00:05 PT.
        (datetime(2026, 11, 10, 10, 2, tzinfo=TZ), datetime(2026, 11, 10, 10, 5, tzinfo=TZ)),
        # 00:06 PST — уже после окна → следующая полночь.
        (datetime(2026, 11, 10, 10, 6, tzinfo=TZ), datetime(2026, 11, 11, 10, 5, tzinfo=TZ)),
        (datetime(2026, 11, 9, 14, 0, tzinfo=TZ), datetime(2026, 11, 10, 10, 5, tzinfo=TZ)),
    ],
)
def test_q3_next_quota_reset_near_midnight(now, expected):
    from digest.worker import next_quota_reset

    assert next_quota_reset(now) == expected
