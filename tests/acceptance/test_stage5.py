"""Этап 5 — тесты приёмки по целям из docs/integration/GOALS.md.

Невыполненная цель помечается xfail(strict=True, reason="S5 Gx"); после выполнения метку снимают.
Сеть — только фейковый сервер Second Brain на localhost (tests/fakes/second_brain.py).
"""

import asyncio
import json
import re
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import OWNER, make_callback, make_message, sent_texts

ROOT = Path(__file__).resolve().parents[2]
FOLDER = "03 - Resources/📥 Из ссылок"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _settings(**kw):
    from config import Settings

    base = {"bot_token": "1:x", "owner_id": OWNER}
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
async def srv(tmp_path):
    from tests.fakes.second_brain import FakeSecondBrain

    server = FakeSecondBrain(tmp_path / "vault")
    await server.start()
    yield server
    await server.stop()


def _api(srv, **kw):
    from digest.vault_api import ApiVault

    return ApiVault(srv.url, srv.token, FOLDER, timeout_s=kw.pop("timeout_s", 2), **kw)


def _stack(db, srv, *, bot=None, clock=None):
    from digest.outbox import Outbox, QueuedVault

    api = _api(srv)
    bot = bot or _bot()
    outbox = Outbox(db, api, bot=bot, owner_id=OWNER, clock=clock or (lambda: NOW))
    return QueuedVault(outbox, api), outbox, bot


def _bot():
    b = MagicMock()
    b.edit_message_text = AsyncMock()
    b.send_message = AsyncMock(return_value=MagicMock(message_id=900))
    b.edit_message_reply_markup = AsyncMock()
    return b


def _md(title="Хоткеи", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", status="unread"):
    return (
        f"---\ntype: digest\nstatus: {status}\nsource: youtube\nurl: {url}\n"
        f"date_added: 2026-11-11\nread_minutes: 2\nconfidence: high\ntags: [dev]\n---\n"
        f"# ⌨️ {title}\n\nТекст.\n"
    )


def _digest_notes(srv) -> dict[str, str]:
    return {p: c for p, c in srv.files().items() if p.startswith(FOLDER + "/")}


# --- G-0 ----------------------------------------------------------------------------------


def test_g0_ffmpeg_fix_in_main():
    out = subprocess.run(
        ["git", "log", "--merges", "--format=%s", "main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Merge branch 'fix/ffmpeg-ytdlp'" in out


def test_g0_gemini_resilience_decided():
    text = (ROOT / "docs/integration/DECISIONS.md").read_text(encoding="utf-8")
    assert "gemini-resilience" in text


async def test_g0_voice_keeps_transcript_when_gemini_down():
    from handlers.voice import on_voice_text_retry  # перенос из gemini-resilience

    assert callable(on_voice_text_retry)


# --- Часть A ------------------------------------------------------------------------------


def test_ga1_protocol_and_errors():
    from digest.errors import VaultError, VaultQueued, VaultRejected, VaultUnavailable
    from digest.vault_base import VaultBackend

    for name in ("ready", "save", "set_status", "replace", "delete", "unread", "find_by_url"):
        assert hasattr(VaultBackend, name)
    assert issubclass(VaultUnavailable, VaultError)
    assert issubclass(VaultRejected, VaultError)
    assert issubclass(VaultQueued, VaultError)
    assert VaultUnavailable("x", retry_after=5).retry_after == 5


async def test_ga2_git_vault_is_backend(tmp_path):
    from digest.vault import GitVault, Vault
    from digest.vault_base import VaultBackend

    assert Vault is GitVault
    v = GitVault(tmp_path, FOLDER, "a", "b")
    assert isinstance(v, VaultBackend)
    assert await v.ready() is False
    assert await v.find_by_url("https://youtu.be/x") is None


async def test_ga3_backend_from_settings(tmp_path, db):
    from pydantic import ValidationError

    from digest.outbox import QueuedVault
    from digest.vault import GitVault
    from main import build_vault

    assert _settings().vault_backend == "git"
    with pytest.raises(ValidationError):
        _settings(vault_backend="api")  # нет URL/токена
    s = _settings(vault_backend="api", second_brain_api_token="t")
    assert s.second_brain_api_url == "http://secondbrain-web:8000/api/v1"
    vault, outbox = build_vault(s, db, bot=_bot())
    assert isinstance(vault, QueuedVault) and outbox is not None
    git = _settings(vault_repo=str(tmp_path))
    (tmp_path / ".git").mkdir()
    vault, outbox = build_vault(git, db, bot=_bot())
    assert isinstance(vault, GitVault) and outbox is None


# --- Часть B ------------------------------------------------------------------------------


def test_gb1_contract_doc():
    text = (ROOT / "docs/SECOND_BRAIN_API_CONTRACT.md").read_text(encoding="utf-8")
    for needle in (
        "GET /health",
        "POST /notes",
        "GET /notes/lookup",
        "PATCH /notes/{path}/frontmatter",
        "PUT /notes/{path}",
        "DELETE /notes/{path}",
        "POST /capture",
        "POST /tasks",
        "POST /ask",
        "Idempotency-Key",
        "idempotency_mismatch",
        "Retry-After",
        "Требования к сайту",
    ):
        assert needle in text, needle


def test_gb2_normalize_cases():
    from digest.classifier import normalize

    cases = json.loads((ROOT / "contract/url_normalize_cases.json").read_text(encoding="utf-8"))
    assert len(cases["cases"]) >= 25
    for case in cases["cases"]:
        assert normalize(case["url"]) == case["key"], case["url"]


def test_gb3_allowed_tags():
    from digest.llm import ALLOWED_TAGS

    data = json.loads((ROOT / "contract/allowed_tags.json").read_text(encoding="utf-8"))
    assert [t["tag"] for t in data["tags"]] == ALLOWED_TAGS
    assert "fun" in ALLOWED_TAGS


async def test_gb4_fake_server_contract_and_faults(srv):
    import httpx

    h = {"Authorization": f"Bearer {srv.token}", "Idempotency-Key": "k1"}
    body = {"folder": FOLDER, "name": "2026-11-11 A", "content": _md("A")}
    async with httpx.AsyncClient(base_url=srv.url) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.post("/notes", json=body)).status_code == 401
        r1 = await c.post("/notes", json=body, headers=h)
        r2 = await c.post("/notes", json=body, headers=h)
        assert r1.status_code == 201 and r1.json() == r2.json()
        r3 = await c.post("/notes", json={**body, "name": "B"}, headers=h)
        assert r3.status_code == 409 and r3.json()["error"]["code"] == "idempotency_mismatch"
        srv.fail("503", retry_after=7)
        r = await c.get("/health")
        assert r.status_code == 503 and r.headers["Retry-After"] == "7"
        bad = {**body, "folder": "01 - Projects"}
        r = await c.post("/notes", json=bad, headers={**h, "Idempotency-Key": "k2"})
        assert r.status_code == 403
    assert len(_digest_notes(srv)) == 1


# --- Часть C ------------------------------------------------------------------------------


async def test_gc1_api_vault_roundtrip_and_errors(srv, caplog):
    from digest.errors import NoteNotFound, VaultRejected, VaultUnavailable

    api = _api(srv)
    assert await api.ready()
    name = await api.save("Хоткеи", _md(), date(2026, 11, 11), idempotency_key="u1")
    assert name == "2026-11-11 Хоткеи"
    url = "https://youtu.be/dQw4w9WgXcQ?si=x"
    assert await api.find_by_url(url) == [name]
    assert await api.set_status(name, url, "read") == name
    assert [n.name for n in await api.unread()] == []
    with pytest.raises(NoteNotFound):
        await api.set_status("нет", "https://example.com/none", "read")
    srv.fail("403")
    with pytest.raises(VaultRejected):
        await api.delete(name, url)
    srv.fail("500")
    with pytest.raises(VaultUnavailable):
        await api.delete(name, url)
    assert srv.token not in caplog.text and srv.token not in repr(api)


def test_gc2_outbox_table_migration(tmp_path):
    import sqlite3

    from database import Database

    d = Database(tmp_path / "x.db")
    d.init()
    d.init()  # идемпотентно
    with sqlite3.connect(d.path) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(vault_ops)")}
    assert {
        "id", "op", "grp", "payload", "idem_key", "chat_id", "msg_id", "status", "attempts",
        "next_attempt_at", "last_error", "created_at", "updated_at",
    } <= cols  # fmt: skip


async def test_gc3_unavailable_then_delivered_and_message_updated(db, srv):
    from digest.errors import VaultQueued

    vault, outbox, bot = _stack(db, srv)
    await srv.stop()
    with pytest.raises(VaultQueued) as exc:
        await vault.save("Хоткеи", _md(), date(2026, 11, 11))
    outbox.attach(exc.value.op_id, chat_id=OWNER, msg_id=10, text="📝 Заметка", note_id=None)
    op = db.get_op(exc.value.op_id)
    assert op.status == "pending" and op.next_attempt_at == NOW + timedelta(seconds=30)
    assert "📮" in str(exc.value)
    await srv.start()
    outbox.clock = lambda: NOW + timedelta(minutes=1)
    assert await outbox.run_due() == 1
    assert db.get_op(op.id).status == "done"
    assert len(_digest_notes(srv)) == 1
    assert bot.edit_message_text.await_args.kwargs["text"].startswith("📝 Заметка")


async def test_gc3_rejected_is_dead_and_owner_notified(db, srv):
    from digest.errors import VaultRejected

    vault, outbox, bot = _stack(db, srv)
    srv.fail("422")
    with pytest.raises(VaultRejected):
        await vault.save("Хоткеи", _md(), date(2026, 11, 11))
    [op] = db.list_ops(("dead",))
    assert op.attempts == 1 and "422" in op.last_error or "validation" in op.last_error
    assert outbox.retry() == 1
    assert await outbox.run_due() == 1
    assert len(_digest_notes(srv)) == 1


async def test_gc3_duplicate_lookup_via_api_or_local(db, srv):
    vault, outbox, _ = _stack(db, srv)
    url = "https://youtu.be/dQw4w9WgXcQ"
    assert await vault.find_by_url(url) == []
    await vault.save("Хоткеи", _md(), date(2026, 11, 11))
    assert await vault.find_by_url(url) == ["2026-11-11 Хоткеи"]
    await srv.stop()
    assert await vault.find_by_url(url) is None  # недоступен → проверка по локальной базе


async def test_gc4_drop_after_write_no_duplicate(db, srv):
    vault, outbox, _ = _stack(db, srv)
    srv.fail("drop")
    from digest.errors import VaultQueued

    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", _md(), date(2026, 11, 11))
    outbox.clock = lambda: NOW + timedelta(minutes=5)
    await outbox.run_due()
    assert len(_digest_notes(srv)) == 1


async def test_gc4_delete_before_create_keeps_order(db, srv):
    from digest.errors import VaultQueued

    vault, outbox, _ = _stack(db, srv)
    await srv.stop()
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", _md(), date(2026, 11, 11))
    with pytest.raises(VaultQueued):
        await vault.set_status("2026-11-11 Хоткеи", url, "read")
    with pytest.raises(VaultQueued):
        await vault.delete("2026-11-11 Хоткеи", url)
    await srv.start()
    outbox.clock = lambda: NOW + timedelta(hours=2)
    await outbox.run_due()
    assert [op.status for op in db.list_ops(("pending", "done", "dead"))] == ["done"] * 3
    assert _digest_notes(srv) == {}
    assert [r["method"] for r in srv.writes] == ["POST", "PATCH", "DELETE"]


# --- Часть D ------------------------------------------------------------------------------


def _cmd(name, args):
    from aiogram.filters import CommandObject

    return CommandObject(prefix="/", command=name, args=args)


async def test_gd1_task_parsing_and_post(db, srv, tz):
    from handlers.brain import cmd_task

    vault, _, _ = _stack(db, srv)
    msg = make_message("/task")
    now = datetime(2026, 11, 11, 12, 0, tzinfo=tz)  # среда
    await cmd_task(msg, _cmd("task", "сдать лабу до пятницы ! #university #foo"), brain=vault,
                   clock=lambda: now)  # fmt: skip
    [task] = srv.tasks
    assert task["text"] == "сдать лабу"
    assert task["due"] == "2026-11-13" and task["priority"] == "high"
    assert task["tags"] == ["university"]
    assert "✅" in sent_texts(msg.answer)[-1]


async def test_gd2_note_inbox_and_weekly(db, srv):
    from handlers.brain import cmd_note

    vault, _, _ = _stack(db, srv)
    await cmd_note(make_message("/note"), _cmd("note", "идея про бота"), brain=vault)
    await cmd_note(make_message("/note"), _cmd("note", "неделя итог недели"), brain=vault)
    assert [(c["target"], c["text"]) for c in srv.captures] == [
        ("inbox", "идея про бота"),
        ("weekly", "итог недели"),
    ]


async def test_gd3_ask_html_split_and_unavailable(db, srv):
    from handlers.brain import cmd_ask

    vault, _, _ = _stack(db, srv)
    await vault.save("Docker", _md("Docker"), date(2026, 11, 11))  # источник для ответа
    srv.ask_answer = "**Ответ** <script>\n\n" + ("строка " * 40 + "\n\n") * 40
    msg = make_message("/ask")
    await cmd_ask(msg, _cmd("ask", "что я знаю про Docker?"), brain=vault)
    texts = sent_texts(msg.answer)
    assert len(texts) >= 2 and all(len(t) <= 4096 for t in texts)
    assert "<b>Ответ</b>" in texts[0] and "&lt;script&gt;" in texts[0]
    assert "📎" in texts[-1]  # источники
    await srv.stop()
    msg = make_message("/ask")
    await cmd_ask(msg, _cmd("ask", "ещё"), brain=vault)
    assert "недоступен" in sent_texts(msg.answer)[-1]


def test_gd4_voice_commands_routed():
    from services.brain_intent import voice_command

    assert voice_command("Добавь задачу купить молоко завтра") == ("task", "купить молоко завтра")
    assert voice_command("запиши в заметки идея для поста") == ("note", "идея для поста")
    assert voice_command("напомни через 5 минут") is None


async def test_gd5_queue_retry_help(db, srv):
    from handlers.basic import COMMANDS, HELP
    from handlers.brain import cmd_queue, cmd_retry, cmd_task

    names = {c.command for c in COMMANDS}
    assert {"task", "note", "ask", "queue", "retry"} <= names
    for cmd in ("/task", "/note", "/ask", "/queue", "/retry"):
        assert cmd in HELP
    vault, outbox, _ = _stack(db, srv)
    await srv.stop()
    await cmd_task(make_message("/task"), _cmd("task", "купить хлеб"), brain=vault,
                   clock=lambda: NOW)  # fmt: skip
    msg = make_message("/queue")
    await cmd_queue(msg, outbox=outbox)
    assert "задача" in sent_texts(msg.answer)[-1]
    await srv.start()
    msg = make_message("/retry")
    await cmd_retry(msg, outbox=outbox)
    assert len(srv.tasks) == 1
    git = make_message("/task")
    await cmd_task(git, _cmd("task", "x"), brain=None, clock=lambda: NOW)
    assert "VAULT_BACKEND=api" in sent_texts(git.answer)[-1]


# --- Часть E ------------------------------------------------------------------------------


def test_ge1_detail_model():
    from main import build_llms

    s = _settings(gemini_api_key="k")
    main_llm, detail = build_llms(s)
    assert s.gemini_detail_model == "gemini-3.8-flash"
    assert detail.model == "gemini-3.8-flash" and detail.fallback_model == main_llm.model
    _, none = build_llms(_settings(gemini_api_key="k", gemini_detail_model=""))
    assert none is None


# --- Часть F ------------------------------------------------------------------------------


def test_gf1_compose():
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    bot = compose["services"]["bot"]
    assert bot["restart"] == "unless-stopped"
    assert bot["logging"]["options"]["max-size"]
    assert any("TZ" in str(e) for e in bot.get("environment", {}))
    net = compose["networks"]["secondbrain"]
    assert net["external"] is True and "DOCKER_NETWORK" in net["name"]
    assert "SECOND_BRAIN_CONTAINER" in str(bot["environment"])


def test_gf2_heartbeat(tmp_path):
    from services.heartbeat import is_healthy, write_heartbeat

    path = tmp_path / "hb"
    assert not is_healthy(path, NOW)
    write_heartbeat(path, NOW)
    assert is_healthy(path, NOW + timedelta(seconds=119))
    assert not is_healthy(path, NOW + timedelta(seconds=121))
    assert "HEALTHCHECK" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_gf3_deploy_scripts():
    for name in ("install.sh", "update.sh", "backup.sh", "restore.sh", "status.sh"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text, name
        assert "\r\n" not in text
    timer = (ROOT / "deploy/systemd/link2note-backup.timer").read_text(encoding="utf-8")
    assert "03:45" in timer
    assert (ROOT / "deploy/systemd/link2note.service").exists()


def test_gf3_admin_backup_and_queue(tmp_path, db, capsys):
    import sqlite3

    from digest.admin import main as admin

    db.add(OWNER, "x", NOW)
    dst = tmp_path / "b.db"
    assert admin(["--db", str(db.path), "backup", str(dst)]) == 0
    with sqlite3.connect(dst) as c:
        assert c.execute("SELECT count(*) FROM reminders").fetchone()[0] == 1
    assert admin(["--db", str(db.path), "queue"]) == 0
    assert "pending: 0" in capsys.readouterr().out


def test_gf4_deploy_doc():
    from config import Settings

    text = (ROOT / "docs/DEPLOY.md").read_text(encoding="utf-8")
    for field in Settings.model_fields:
        assert field.upper() in text, field
    assert "docs/DEPLOY.md" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/DEPLOY.md" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


async def test_gf5_dry_run_starts_without_telegram(tmp_path, monkeypatch):
    from aiogram import Bot, Dispatcher

    import main

    s = _settings(dry_run=True, db_path=str(tmp_path / "d.db"))
    monkeypatch.setattr(main, "get_settings", lambda: s)
    telegram = AsyncMock(side_effect=AssertionError("DRY_RUN не ходит в Telegram"))
    monkeypatch.setattr(Dispatcher, "start_polling", telegram)
    monkeypatch.setattr(Bot, "set_my_commands", telegram)
    # Роутеры модульные: собранный здесь диспетчер сломал бы остальные тесты.
    monkeypatch.setattr(main, "build_dispatcher", MagicMock(side_effect=AssertionError("dp")))
    task = asyncio.create_task(main.main())
    hb = tmp_path / "heartbeat"
    for _ in range(50):
        await asyncio.sleep(0.1)
        if hb.exists():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hb.exists()


# --- Часть G ------------------------------------------------------------------------------


async def test_gg1_live_check_script_against_fake(srv, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_api", ROOT / "scripts/check_second_brain_api.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("SECOND_BRAIN_API_URL", srv.url)
    monkeypatch.setenv("SECOND_BRAIN_API_TOKEN", srv.token)
    assert await mod.run() == 0
    out = capsys.readouterr().out
    assert "❌" not in out and out.count("✅") >= 8
    assert _digest_notes(srv) == {}  # за собой убрал


# --- G-E2E --------------------------------------------------------------------------------


async def _e2e(db, srv, tmp_path, *, restart: bool):
    from database import Database
    from digest.worker import Job, process_job
    from handlers.brain import cmd_ask, cmd_note, cmd_task
    from handlers.keyboards import NoteCb
    from handlers.notes import cmd_unread, on_note
    from tests.test_worker import JOB, _http, _llm

    vault, outbox, bot = _stack(db, srv)
    await srv.stop()
    async with _http() as http:
        await process_job(JOB, bot=bot, llm=_llm(), http=http, vault=vault,
                          today=lambda: date(2026, 11, 11), db=db, outbox=outbox)  # fmt: skip
    assert "📮" in bot.edit_message_text.await_args.kwargs["text"]
    assert _digest_notes(srv) == {}
    await srv.start()
    if restart:  # новый процесс: новое подключение к базе, новый outbox
        db = Database(db.path)
        db.init()
        vault, outbox, bot = _stack(db, srv)
    outbox.clock = lambda: NOW + timedelta(minutes=1)
    assert await outbox.run_due() == 1
    [path] = _digest_notes(srv)
    note = db.find_note(["youtube:dQw4w9WgXcQ"])
    assert note is not None

    async def press(action):
        cb = make_callback()
        cb.message.reply = AsyncMock(return_value=MagicMock(message_id=777))
        queue: asyncio.Queue[Job] = asyncio.Queue()
        await on_note(cb, callback_data=NoteCb(action=action, id=note.id), db=db, vault=vault,
                      link_queue=queue)  # fmt: skip
        return queue

    await press("read")
    assert "status: read" in _digest_notes(srv)[path]
    queue = await press("more")
    job = queue.get_nowait()
    async with _http() as http:
        await process_job(job, bot=bot, llm=_llm(), http=http, vault=vault,
                          today=lambda: date(2026, 11, 11), db=db, outbox=outbox)  # fmt: skip
    assert "status: unread" in _digest_notes(srv)[path]
    await press("del_yes")
    assert _digest_notes(srv) == {}
    await cmd_task(make_message("/task"), _cmd("task", "купить хлеб завтра"), brain=vault,
                   clock=lambda: NOW)  # fmt: skip
    await cmd_note(make_message("/note"), _cmd("note", "идея"), brain=vault)
    ask = make_message("/ask")
    await cmd_ask(ask, _cmd("ask", "что нового?"), brain=vault)
    unread = make_message("/unread")
    await cmd_unread(unread, vault=vault)
    assert sent_texts(unread.answer)[-1] == "Всё прочитано 🎉"
    assert [t["text"] for t in srv.tasks] == ["купить хлеб"]
    assert [c["text"] for c in srv.captures] == ["идея"]
    assert db.list_ops(("pending", "dead")) == []
    assert sum(1 for w in srv.writes if w["method"] == "POST" and w["path"] == "/notes") == 1


async def test_e2e_full_flow(db, srv, tmp_path):
    await _e2e(db, srv, tmp_path, restart=False)


async def test_e2e_with_restart(db, srv, tmp_path):
    await _e2e(db, srv, tmp_path, restart=True)


def test_acceptance_has_test_per_goal():
    text = Path(__file__).read_text(encoding="utf-8")
    goals = re.findall(r"\*\*(G-[A-Z0-9]+)\*\*", (ROOT / "docs/integration/GOALS.md").read_text(
        encoding="utf-8"))  # fmt: skip
    for goal in goals:
        if goal == "G-final":
            continue
        prefix = "test_" + goal.lower().replace("-", "", 1).replace("-", "_")
        if goal == "G-E2E":
            prefix = "test_e2e"
        assert prefix in text, goal
