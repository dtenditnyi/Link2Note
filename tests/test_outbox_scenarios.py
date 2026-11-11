"""G-C4: сценарии сбоев офлайн-очереди против фейкового сайта + стресс со случайными сбоями.

Главный риск этапа: потерять операцию или создать дубль. Каждый сценарий заканчивается
проверкой состояния «хранилища» сайта и числа реально выполненных записей.
"""

import asyncio
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from database import Database
from digest.errors import VaultQueued
from digest.outbox import Outbox, QueuedVault
from digest.vault_api import ApiVault
from tests.fakes.second_brain import FOLDER, FakeSecondBrain

DAY = date(2026, 11, 11)
T0 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def md(title, url, status="unread", body="Текст"):
    return (
        f"---\ntype: digest\nstatus: {status}\nsource: article\nurl: {url}\n"
        f"date_added: 2026-11-11\nread_minutes: 2\ntags: [dev]\n---\n# 📝 {title}\n\n{body}\n"
    )


class Clock:
    def __init__(self):
        self.at = T0

    def __call__(self):
        return self.at


@pytest.fixture
async def srv(tmp_path):
    async with FakeSecondBrain(tmp_path / "vault", timeout_delay=0.4) as s:
        yield s


def make(db, srv, clock, *, timeout_s=1.0):
    api = ApiVault(srv.url, srv.token, FOLDER, timeout_s=timeout_s)
    outbox = Outbox(db, api, clock=clock, max_attempts=1000, max_age=timedelta(days=365))
    return QueuedVault(outbox, api), outbox


def notes(srv):
    return {p: c for p, c in srv.files().items() if p.startswith(FOLDER + "/")}


def posts(srv):
    return sum(1 for w in srv.writes if w["method"] == "POST" and w["path"] == "/notes")


async def drain(outbox, clock, rounds=60):
    for _ in range(rounds):
        if not outbox.db.list_ops(("pending",)):
            return
        clock.at += timedelta(hours=2)
        await outbox.run_due()
    raise AssertionError(f"очередь не опустела: {outbox.db.list_ops(('pending',))}")


async def test_server_off_then_on_exactly_one_note(db, srv):
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    await srv.stop()
    with pytest.raises(VaultQueued):
        await vault.save("Статья", md("Статья", "https://a.com/1"), DAY)
    for _ in range(3):  # сайт ещё лежит — повторы не плодят записей
        clock.at += timedelta(hours=1)
        await outbox.run_due()
    await srv.start()
    await drain(outbox, clock)
    assert list(notes(srv)) == [f"{FOLDER}/2026-11-11 Статья.md"] and posts(srv) == 1


async def test_500_three_times_then_success(db, srv):
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    srv.fail("500", times=3)
    with pytest.raises(VaultQueued) as err:
        await vault.save("Статья", md("Статья", "https://a.com/1"), DAY)
    await drain(outbox, clock)
    op = db.get_op(err.value.op_id)
    assert op.status == "done" and op.attempts == 4 and posts(srv) == 1


async def test_429_waits_retry_after(db, srv):
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    srv.fail("429", retry_after=600)
    with pytest.raises(VaultQueued) as err:
        await vault.capture("идея", "inbox")
    clock.at += timedelta(seconds=599)
    await outbox.run_due()
    assert srv.captures == []
    clock.at += timedelta(seconds=2)
    await outbox.run_due()
    assert db.get_op(err.value.op_id).status == "done" and len(srv.captures) == 1


async def test_timeout_late_write_no_duplicate(db, srv):
    clock = Clock()
    vault, outbox = make(db, srv, clock, timeout_s=0.1)
    srv.fail("timeout")
    with pytest.raises(VaultQueued):
        await vault.save("Статья", md("Статья", "https://a.com/1"), DAY)
    await asyncio.sleep(0.5)  # сервер дописал после таймаута клиента
    await drain(outbox, clock)
    assert len(notes(srv)) == 1 and posts(srv) == 1


@pytest.mark.parametrize("kind", ["403", "422"])
async def test_rejected_dead_no_retries(db, srv, kind):
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    srv.fail(kind)
    with pytest.raises(Exception, match=kind):
        await vault.save("Статья", md("Статья", "https://a.com/1"), DAY)
    requests = len(srv.requests)
    for _ in range(5):
        clock.at += timedelta(hours=2)
        await outbox.run_due()
    assert len(srv.requests) == requests and notes(srv) == {}


async def test_restart_mid_queue(db, srv):
    clock = Clock()
    vault, _ = make(db, srv, clock)
    await srv.stop()
    for i in range(4):
        with pytest.raises(VaultQueued):
            await vault.save(f"Статья {i}", md(f"Статья {i}", f"https://a.com/{i}"), DAY)
    with pytest.raises(VaultQueued):
        await vault.set_status("2026-11-11 Статья 1", "https://a.com/1", "read")
    await srv.start()
    srv.fail("503", times=2)
    db2 = Database(db.path)  # новый процесс
    db2.init()
    _, outbox2 = make(db2, srv, clock)
    await drain(outbox2, clock)
    assert len(notes(srv)) == 4 and posts(srv) == 4
    assert "status: read" in notes(srv)[f"{FOLDER}/2026-11-11 Статья 1.md"]


async def test_five_links_at_once(db, srv):
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    await srv.stop()

    async def save(i):
        with pytest.raises(VaultQueued):
            await vault.save(f"Видео {i}", md(f"Видео {i}", f"https://v.com/{i}"), DAY)

    await asyncio.gather(*(save(i) for i in range(5)))
    await srv.start()
    await drain(outbox, clock)
    assert sorted(notes(srv)) == [f"{FOLDER}/2026-11-11 Видео {i}.md" for i in range(5)]


async def test_concurrent_saves_with_server_up(db, srv):
    clock = Clock()
    vault, _ = make(db, srv, clock)
    names = await asyncio.gather(
        *(vault.save(f"Видео {i}", md(f"Видео {i}", f"https://v.com/{i}"), DAY) for i in range(5))
    )
    assert sorted(names) == [f"2026-11-11 Видео {i}" for i in range(5)] and posts(srv) == 5


async def test_big_note_cyrillic_emoji_name(db, srv):
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    title = "🔥 Ёлка: «Docker» и Proxmox — ускоряем 10× 🚀"
    big = md(title, "https://a.com/big", body="Длинный текст 🚀 " * 60000)  # ~1 МБ
    await srv.stop()
    with pytest.raises(VaultQueued) as err:
        await vault.save(title, big, DAY)
    await srv.start()
    await drain(outbox, clock)
    name = db.get_op(err.value.op_id).result["name"]
    assert name == "2026-11-11 🔥 Ёлка «Docker» и Proxmox — ускоряем 10× 🚀"
    assert notes(srv)[f"{FOLDER}/{name}.md"] == big


async def test_times_are_aware_utc(db, srv):
    from zoneinfo import ZoneInfo

    clock = Clock()
    clock.at = datetime(2026, 11, 11, 12, 0, tzinfo=ZoneInfo("Europe/Bucharest"))
    vault, _ = make(db, srv, clock)
    await srv.stop()
    with pytest.raises(VaultQueued) as err:
        await vault.capture("x", "inbox")
    op = db.get_op(err.value.op_id)
    for at in (op.created_at, op.updated_at, op.next_attempt_at):
        assert at.utcoffset() == timedelta(0)
    assert op.created_at == datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


# --- стресс: случайные сбои ---------------------------------------------------------------

FAULTS = ["500", "503", "429", "drop", "409", None, None, None]


@pytest.mark.parametrize("seed", range(12))
async def test_stress_random_faults(db, srv, seed):
    """Случайные операции над заметками + случайные сбои/выключения → итог как по модели."""
    rnd = random.Random(seed)
    clock = Clock()
    vault, outbox = make(db, srv, clock)
    expected: dict[str, str | None] = {}  # путь → ожидаемое содержимое (None — удалена)
    current: dict[str, str] = {}  # имя → текущий Markdown (модель)

    async def maybe_fault():
        kind = rnd.choice(FAULTS)
        if kind:
            srv.fail(kind, times=rnd.randint(1, 3), retry_after=rnd.choice([None, 5, 300]))
        if rnd.random() < 0.15 and srv.running:
            await srv.stop()
        elif not srv.running and rnd.random() < 0.5:
            await srv.start()

    async def call(coro):
        try:
            return await coro
        except VaultQueued:
            return None

    for i in range(rnd.randint(3, 6)):
        name = f"2026-11-11 Заметка {seed}-{i}"
        url = f"https://s.com/{seed}/{i}"
        current[name] = md(f"Заметка {seed}-{i}", url)
        await maybe_fault()
        await call(vault.save(f"Заметка {seed}-{i}", current[name], DAY))
        for _ in range(rnd.randint(0, 3)):
            action = rnd.choice(["read", "replace", "delete"])
            await maybe_fault()
            if name not in current:
                break
            if action == "read":
                current[name] = current[name].replace("status: unread", "status: read", 1)
                await call(vault.set_status(name, url, "read"))
            elif action == "replace":
                current[name] = md(f"Заметка {seed}-{i}", url, body=f"подробно {rnd.random()}")
                await call(vault.replace(name, url, current[name]))
            else:
                del current[name]
                await call(vault.delete(name, url))
        expected[f"{FOLDER}/{name}.md"] = current.get(name)
    if not srv.running:
        await srv.start()
    await asyncio.sleep(0.05)
    await drain(outbox, clock)
    assert db.list_ops(("dead",)) == []
    assert notes(srv) == {p: c for p, c in expected.items() if c is not None}
    assert posts(srv) == len(expected)  # ни одного дубля
