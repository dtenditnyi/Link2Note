"""Outbox (G-C2/G-C3): таблица vault_ops, повторы, dead, порядок, перезапуск, /retry."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from database import Database
from digest.errors import NoteNotFound, VaultQueued, VaultRejected
from digest.outbox import BACKOFF_S, Outbox, QueuedVault, describe, note_group
from digest.vault_api import ApiVault
from tests.fakes.second_brain import FOLDER, FakeSecondBrain

DAY = date(2026, 11, 11)
T0 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
CANON = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def md(title="Хоткеи", url=CANON):
    return (
        f"---\ntype: digest\nstatus: unread\nsource: youtube\nurl: {url}\n"
        f"date_added: 2026-11-11\nread_minutes: 2\ntags: [dev]\n---\n# ⌨️ {title}\n"
    )


class Clock:
    def __init__(self, at: datetime = T0):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


@pytest.fixture
async def srv(tmp_path):
    async with FakeSecondBrain(tmp_path / "vault") as s:
        yield s


def _bot():
    b = MagicMock()
    b.edit_message_text = AsyncMock()
    b.send_message = AsyncMock()
    return b


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def stack(db, srv, clock):
    api = ApiVault(srv.url, srv.token, FOLDER, timeout_s=1)
    bot = _bot()
    outbox = Outbox(db, api, bot=bot, owner_id=111, clock=clock, max_attempts=5)
    return QueuedVault(outbox, api), outbox, bot


def notes(srv):
    return {p: c for p, c in srv.files().items() if p.startswith(FOLDER + "/")}


# --- таблица ---------------------------------------------------------------------------


def test_migration_on_old_db_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:  # база этапа 3 без vault_ops
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, note_name TEXT)")
    d = Database(path)
    d.init()
    d.init()
    op = d.add_op("task", "task:1", {"text": "x"}, "k-1", T0)
    row = d.get_op(op)
    assert row.status == "pending" and row.next_attempt_at == T0 and row.attempts == 0
    assert row.created_at.tzinfo is not None


def test_idem_key_unique(db):
    db.add_op("task", "g", {}, "same", T0)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_op("task", "g", {}, "same", T0)


def test_naive_time_rejected(db):
    with pytest.raises(ValueError):
        db.add_op("task", "g", {}, "k", datetime(2026, 11, 11, 10, 0))


def test_due_ops_respects_group_order(db):
    a = db.add_op("save", "note:x", {}, "k1", T0)
    b = db.add_op("set_status", "note:x", {}, "k2", T0)
    c = db.add_op("task", "task:1", {}, "k3", T0)
    assert [o.id for o in db.due_ops(T0)] == [a, c]  # b ждёт a
    db.update_op(a, T0, next_attempt_at=T0 + timedelta(minutes=5))
    assert [o.id for o in db.due_ops(T0)] == [c]  # голова группы ещё не пришла — b тоже ждёт
    db.update_op(a, T0, status="done")
    assert [o.id for o in db.due_ops(T0)] == [b, c]


# --- поведение -------------------------------------------------------------------------


async def test_immediate_success(stack, srv, db):
    vault, _, _ = stack
    assert await vault.save("Хоткеи", md(), DAY) == "2026-11-11 Хоткеи"
    [op] = db.list_ops(("done",))
    assert (
        op.op == "save"
        and op.payload["markdown"] == md()
        and op.result == {"name": "2026-11-11 Хоткеи"}
    )


async def test_unavailable_queues_with_backoff_and_retry_after(stack, srv, db, clock):
    vault, outbox, _ = stack
    srv.fail("503", times=2, retry_after=120)
    with pytest.raises(VaultQueued) as err:
        await vault.save("Хоткеи", md(), DAY)
    assert err.value.note_name == "2026-11-11 Хоткеи"
    op = db.get_op(err.value.op_id)
    assert op.attempts == 1 and op.next_attempt_at == T0 + timedelta(seconds=120)
    assert "503" in op.last_error
    clock.advance(seconds=60)
    assert await outbox.run_due() == 0  # Retry-After ещё не прошёл
    clock.advance(seconds=60)
    assert await outbox.run_due() == 0  # второй 503 → бэкофф 60 с (больше 120 из Retry-After)
    assert db.get_op(op.id).next_attempt_at == clock.at + timedelta(seconds=120)
    clock.advance(seconds=120)
    assert await outbox.run_due() == 1
    assert db.get_op(op.id).status == "done" and len(notes(srv)) == 1


async def test_backoff_schedule(stack, srv, db, clock):
    vault, outbox, _ = stack
    await srv.stop()
    with pytest.raises(VaultQueued) as err:
        await vault.create_task("x", due=None, priority=None, tags=[])
    delays = [(db.get_op(err.value.op_id).next_attempt_at - clock.at).total_seconds()]
    for _ in range(3):
        clock.at = db.get_op(err.value.op_id).next_attempt_at
        await outbox.run_due()
        delays.append((db.get_op(err.value.op_id).next_attempt_at - clock.at).total_seconds())
    assert delays == [float(s) for s in BACKOFF_S[:4]]


async def test_rejected_dead_without_retry(stack, srv, db, clock):
    vault, outbox, bot = stack
    srv.fail("403")
    with pytest.raises(VaultRejected, match="403"):
        await vault.save("Хоткеи", md(), DAY)
    [op] = db.list_ops(("dead",))
    assert op.attempts == 1 and op.result == {"error": "rejected"}
    clock.advance(hours=5)
    assert await outbox.run_due() == 0
    assert notes(srv) == {}
    bot.send_message.assert_not_awaited()  # пользователю ответил вызывающий код


async def test_background_dead_notifies_owner(stack, srv, db, clock):
    vault, outbox, bot = stack
    await srv.stop()
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", md(), DAY)
    await srv.start()
    srv.fail("422")
    clock.advance(minutes=1)
    assert await outbox.run_due() == 1
    text = bot.send_message.await_args.kwargs["text"]
    assert "Не смог отправить" in text and "«2026-11-11 Хоткеи»" in text and "/retry" in text


async def test_max_attempts_dead(stack, srv, db, clock):
    vault, outbox, bot = stack
    await srv.stop()
    with pytest.raises(VaultQueued) as err:
        await vault.capture("идея", "inbox")
    for _ in range(10):
        clock.advance(hours=2)
        await outbox.run_due()
    op = db.get_op(err.value.op_id)
    assert op.status == "dead" and op.attempts == 5 and "5 попыток" in op.last_error
    assert bot.send_message.await_count == 1


async def test_max_age_dead(stack, srv, db, clock):
    vault, outbox, bot = stack
    await srv.stop()
    with pytest.raises(VaultQueued) as err:
        await vault.capture("идея", "inbox")
    clock.advance(days=7, seconds=1)
    assert await outbox.run_due() == 1
    op = db.get_op(err.value.op_id)
    assert op.status == "dead" and op.result == {"error": "expired"}
    bot.send_message.assert_awaited_once()


async def test_retry_revives_dead_and_pending(stack, srv, db, clock):
    vault, outbox, _ = stack
    srv.fail("403")
    with pytest.raises(VaultRejected):
        await vault.capture("a", "inbox")
    await srv.stop()
    with pytest.raises(VaultQueued):
        await vault.capture("b", "inbox")
    await srv.start()
    assert outbox.retry() == 2
    assert await outbox.run_due() == 2
    assert [c["text"] for c in srv.captures] == ["a", "b"]


async def test_retry_restarts_age(stack, srv, db, clock):
    vault, outbox, _ = stack
    await srv.stop()
    with pytest.raises(VaultQueued) as err:
        await vault.capture("a", "inbox")
    clock.advance(days=8)
    outbox.retry()
    await srv.start()
    assert await outbox.run_due() == 1
    assert db.get_op(err.value.op_id).status == "done"


async def test_message_updated_after_delivery_with_site_name(stack, srv, db, clock):
    vault, outbox, bot = stack
    await vault.save("Хоткеи", md(), DAY)  # занимает имя — следующая станет « (2)»
    await srv.stop()
    nid = db.add_note("2026-11-11 Хоткеи", CANON, "youtube", 111)
    with pytest.raises(VaultQueued) as err:
        await vault.save("Хоткеи", md(), DAY)
    vault.attach(err.value.op_id, chat_id=111, msg_id=5, text="📝 Заметка: «2026-11-11 Хоткеи»",
                 note_id=nid)  # fmt: skip
    await srv.start()
    clock.advance(minutes=1)
    await outbox.run_due()
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "📝 Заметка: «2026-11-11 Хоткеи (2)»"
    assert kw["chat_id"] == 111 and kw["message_id"] == 5 and kw["reply_markup"] is not None
    assert db.get_note(nid).note_name == "2026-11-11 Хоткеи (2)"


async def test_later_ops_use_site_name(stack, srv, db, clock):
    vault, outbox, _ = stack
    await vault.save("Хоткеи", md(url="https://other.com/a"), DAY)  # занять имя
    await srv.stop()
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", md(), DAY)
    with pytest.raises(VaultQueued):
        await vault.set_status("2026-11-11 Хоткеи", CANON, "read")
    [_, status_op] = db.list_ops(("pending",))
    await srv.start()
    clock.advance(minutes=1)
    await outbox.run_due()
    assert db.get_op(status_op.id).payload["name"] == "2026-11-11 Хоткеи (2)"
    assert "status: read" in notes(srv)[f"{FOLDER}/2026-11-11 Хоткеи (2).md"]
    assert "status: unread" in notes(srv)[f"{FOLDER}/2026-11-11 Хоткеи.md"]


async def test_not_found_maps_to_note_not_found(stack):
    vault, _, _ = stack
    with pytest.raises(NoteNotFound):
        await vault.set_status("нет", "https://example.com/x", "read")


async def test_delete_missing_raises_not_found(stack):
    vault, _, _ = stack
    with pytest.raises(NoteNotFound):
        await vault.delete("нет", "https://example.com/x")


async def test_find_by_url_api_pending_or_unknown(stack, srv):
    vault, _, _ = stack
    assert await vault.find_by_url(CANON) == []
    await srv.stop()
    assert await vault.find_by_url(CANON) is None
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", md(), DAY)
    await srv.start()
    assert await vault.find_by_url("https://youtu.be/dQw4w9WgXcQ") == ["2026-11-11 Хоткеи"]


async def test_restart_new_outbox_continues(db, srv, clock):
    api = ApiVault(srv.url, srv.token, FOLDER, timeout_s=1)
    vault = QueuedVault(Outbox(db, api, clock=clock), api)
    await srv.stop()
    for i in range(3):
        with pytest.raises(VaultQueued):
            await vault.capture(f"идея {i}", "inbox")
    await srv.start()
    fresh_db = Database(db.path)
    fresh_db.init()
    fresh = Outbox(fresh_db, ApiVault(srv.url, srv.token, FOLDER), clock=clock)
    clock.advance(minutes=1)
    assert await fresh.run_due() == 3
    assert [c["text"] for c in srv.captures] == ["идея 0", "идея 1", "идея 2"]


async def test_run_forever_wakes_on_enqueue(db, srv, clock):
    import asyncio

    api = ApiVault(srv.url, srv.token, FOLDER, timeout_s=1)
    outbox = Outbox(db, api, clock=clock)
    task = asyncio.create_task(outbox.run_forever(idle_s=30))
    await asyncio.sleep(0.05)
    outbox.enqueue("capture", {"text": "x", "target": "inbox"}, grp="capture:1")
    for _ in range(50):
        await asyncio.sleep(0.05)
        if srv.captures:
            break
    task.cancel()
    assert [c["text"] for c in srv.captures] == ["x"]


def test_describe_and_group():
    from database import OpRow

    def row(op, **p):
        return OpRow(1, op, "g", p, "k", None, None, None, "pending", 0, T0, None, None, T0, T0,
                     None)  # fmt: skip

    assert describe(row("task", text="хлеб")) == "задача «хлеб»"
    assert describe(row("capture", target="weekly")) == "запись в недельную заметку"
    assert note_group("https://youtu.be/dQw4w9WgXcQ?si=1") == note_group(CANON)
    assert note_group("", "Имя") == "note:name:Имя"


# --- ревью C/D/E ------------------------------------------------------------------------


def test_backoff_schedule_matches_spec():
    """Мутация (c2) выживала: расписание сверяем со спецификацией, а не с самим модулем."""
    assert BACKOFF_S == (30, 60, 300, 900, 3600)


async def test_attempt_uses_site_name_for_later_ops_no_foreign_delete(stack, srv, db, clock):
    """#1: save получил « (2)» внутри attempt — delete не должен удалить чужую заметку."""
    vault, outbox, _ = stack
    await vault.save("Хоткеи", md(url="https://other.com/a"), DAY)  # чужая, занимает имя
    await srv.stop()
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", md(), DAY)
    with pytest.raises(VaultQueued):
        await vault.delete("2026-11-11 Хоткеи", CANON)
    await srv.start()
    # Ещё одно нажатие той же заметки → attempt по группе: save « (2)», delete « (2)»,
    # а «Прочитано» уже удалённой заметки — честное «не нашёл».
    with pytest.raises(NoteNotFound):
        await vault.set_status("2026-11-11 Хоткеи", CANON, "read")
    await outbox.run_due()
    assert list(notes(srv)) == [f"{FOLDER}/2026-11-11 Хоткеи.md"]
    assert "other.com" in notes(srv)[f"{FOLDER}/2026-11-11 Хоткеи.md"]


async def test_locate_by_name_checks_url(stack, srv):
    """#1, вторая линия: заметка с тем же именем, но другой ссылкой — не наша."""
    vault, _, _ = stack
    await vault.save("Хоткеи", md(url="https://other.com/a"), DAY)
    with pytest.raises(NoteNotFound):
        await vault.delete("2026-11-11 Хоткеи", CANON)
    assert len(notes(srv)) == 1


async def test_earlier_group_op_dead_in_attempt_notifies(stack, srv, db, clock):
    """#2: save умер во время нажатия «Прочитано» — владелец узнаёт, сообщение «📮» обновлено."""
    vault, outbox, bot = stack
    await srv.stop()
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", md(), DAY)
    await srv.start()
    srv.fail("422", method="POST")
    with pytest.raises(NoteNotFound):
        await vault.set_status("2026-11-11 Хоткеи", CANON, "read")
    bot.send_message.assert_awaited_once()
    assert "«2026-11-11 Хоткеи»" in bot.send_message.await_args.kwargs["text"]


async def test_retry_does_not_revive_superseded_dead_save(stack, srv, db, clock):
    """#5: save dead, а delete той же заметки уже выполнен — /retry не воскрешает заметку."""
    vault, outbox, _ = stack
    srv.fail("422", method="POST")
    with pytest.raises(VaultRejected):
        await vault.save("Хоткеи", md(), DAY)
    with pytest.raises(NoteNotFound):  # заметки нет — delete «выполнен»
        await vault.delete("2026-11-11 Хоткеи", CANON)
    assert outbox.retry() == 0
    await outbox.run_due()
    assert notes(srv) == {}


async def test_unexpected_errors_limited_by_max_attempts(stack, db, clock, monkeypatch):
    """#6: ошибка бота повторяется не бесконечно (до OUTBOX_MAX_AGE), а до max_attempts."""
    vault, outbox, bot = stack

    async def boom(op):
        raise RuntimeError("bug")

    monkeypatch.setitem(outbox._handlers, "capture", boom)
    with pytest.raises(VaultQueued) as err:
        await vault.capture("x", "inbox")
    for _ in range(10):
        clock.advance(hours=2)
        await outbox.run_due()
    op = db.get_op(err.value.op_id)
    assert op.status == "dead" and op.attempts == 5
    bot.send_message.assert_awaited_once()


async def test_pending_delete_is_not_duplicate(stack, srv):
    """#7: ссылка пришла снова, пока удаление ждёт в очереди — это не «♻️ Уже есть»."""
    vault, _, _ = stack
    await vault.save("Хоткеи", md(), DAY)
    srv.fail("503", method="DELETE")
    with pytest.raises(VaultQueued):
        await vault.delete("2026-11-11 Хоткеи", CANON)
    assert await vault.find_by_url(CANON) == []


async def test_drop_after_write_retries_with_same_key(stack, srv, db, clock):
    """Мутация (b): новый ключ на попытку дал бы дубль — детерминированная проверка."""
    vault, outbox, _ = stack
    srv.fail("drop", method="POST")
    with pytest.raises(VaultQueued):
        await vault.save("Хоткеи", md(), DAY)
    clock.advance(minutes=1)
    await outbox.run_due()
    assert list(notes(srv)) == [f"{FOLDER}/2026-11-11 Хоткеи.md"]


async def test_already_read_sends_no_patch(stack, srv):
    """Мутация (g): статус уже нужный — PATCH не отправляется вовсе."""
    vault, _, _ = stack
    await vault.save("Хоткеи", md().replace("status: unread", "status: read"), DAY)
    await vault.set_status("2026-11-11 Хоткеи", CANON, "read")
    assert not [r for r in srv.requests if r["method"] == "PATCH"]


async def test_background_loop_skips_op_being_sent_by_press(stack, srv, db, clock):
    """Финальная проверка #3: фон не берёт операцию, которую сейчас отправляет нажатие."""
    vault, outbox, bot = stack
    op_id = outbox.enqueue("capture", {"text": "x", "target": "inbox"}, grp="capture:1")
    outbox._claimed.add(op_id)
    assert await outbox.run_due() == 0
    outbox._claimed.discard(op_id)
    assert await outbox.run_due() == 1


def test_default_attempts_cover_max_age():
    """Финальная проверка #1: при умолчаниях операция ждёт сайт ~OUTBOX_MAX_AGE, а не сутки."""
    from config import Settings

    s = Settings(_env_file=None, bot_token="1:x", owner_id=1)
    waited = sum(BACKOFF_S) + (s.outbox_max_attempts - len(BACKOFF_S)) * BACKOFF_S[-1]
    assert waited >= s.outbox_max_age.total_seconds()
