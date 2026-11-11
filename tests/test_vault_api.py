"""ApiVault против фейкового сайта Second Brain (настоящий HTTP на localhost)."""

import logging
from datetime import date

import httpx
import pytest

from digest.errors import NoteAmbiguous, NoteNotFound, VaultRejected, VaultUnavailable
from digest.vault_api import ApiVault, note_key
from digest.vault_base import VaultBackend
from tests.fakes.second_brain import FOLDER, FakeSecondBrain

DAY = date(2026, 11, 11)
CANON = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
RAW = "https://youtu.be/dQw4w9WgXcQ?si=abc"


def md(title="Хоткеи", url=CANON, status="unread", added="2026-11-11", body="Текст"):
    return (
        f"---\ntype: digest\nstatus: {status}\nsource: youtube\nurl: {url}\nauthor: Fireship\n"
        f"date_added: {added}\nread_minutes: 3\nconfidence: high\nmodel: gemini-x\n"
        f"tags: [dev]\n---\n# ⌨️ {title}\n\n{body}\n"
    )


@pytest.fixture
async def srv(tmp_path):
    async with FakeSecondBrain(tmp_path / "vault") as s:
        yield s


@pytest.fixture
async def api(srv):
    v = ApiVault(srv.url, srv.token, FOLDER, timeout_s=1)
    yield v
    await v.aclose()


def notes(srv):
    return {p: c for p, c in srv.files().items() if p.startswith(FOLDER + "/")}


async def test_is_backend(api):
    assert isinstance(api, VaultBackend)


async def test_ready_true_and_false(api, srv):
    assert await api.ready() is True
    await srv.stop()
    assert await api.ready() is False


async def test_save_sends_markdown_as_is_and_source(api, srv):
    name = await api.save("Хоткеи: VS Code?", md(), DAY)
    assert name == "2026-11-11 Хоткеи VS Code"
    assert notes(srv) == {f"{FOLDER}/2026-11-11 Хоткеи VS Code.md": md()}


async def test_save_name_collision_returns_site_name(api):
    await api.save("Хоткеи", md(), DAY)
    assert await api.save("Хоткеи", md(), DAY) == "2026-11-11 Хоткеи (2)"


async def test_save_same_key_twice_no_duplicate(api, srv):
    a = await api.save("Хоткеи", md(), DAY, idempotency_key="11111111-1111-4111-8111-111111111111")
    b = await api.save("Хоткеи", md(), DAY, idempotency_key="11111111-1111-4111-8111-111111111111")
    assert a == b and len(notes(srv)) == 1


async def test_every_write_has_idempotency_key(api, srv):
    seen: list[str | None] = []
    orig = api._client.request

    async def spy(method, url, **kw):
        if method != "GET":
            seen.append(kw["headers"].get("Idempotency-Key"))
        return await orig(method, url, **kw)

    api._client.request = spy
    name = await api.save("Хоткеи", md(), DAY)
    await api.set_status(name, CANON, "read")
    await api.replace(name, CANON, md(body="подробно"))
    await api.delete(name, CANON)
    await api.create_task("задача")
    await api.capture("идея", "inbox")
    assert len(seen) == 6 and all(seen)


async def test_set_status_patch_only_status(api, srv):
    name = await api.save("Хоткеи", md(body="status: unread в тексте"), DAY)
    assert await api.set_status(name, RAW, "read") == name
    [content] = notes(srv).values()
    assert content == md(body="status: unread в тексте", status="read")


async def test_set_status_already_read_sends_nothing(api, srv):
    name = await api.save("Хоткеи", md(status="read"), DAY)
    before = len(srv.writes)
    await api.set_status(name, CANON, "read")
    assert len(srv.writes) == before


async def test_renamed_note_found_by_url(api, srv):
    await api.save("Хоткеи", md(), DAY)
    old = srv.root / FOLDER / "2026-11-11 Хоткеи.md"
    new = srv.root / FOLDER / "Темы" / "Мои хоткеи.md"
    new.parent.mkdir(parents=True)
    old.rename(new)
    assert await api.set_status("2026-11-11 Хоткеи", RAW, "read") == "Мои хоткеи"
    assert "status: read" in new.read_text(encoding="utf-8")


async def test_note_moved_out_of_bot_folder_is_rejected(api, srv):
    """Права токена — только папка бота (D13): перенесённую в архив заметку бот не меняет."""
    await api.save("Хоткеи", md(), DAY)
    new = srv.root / "05 - Archive" / "Мои хоткеи.md"
    new.parent.mkdir(parents=True)
    (srv.root / FOLDER / "2026-11-11 Хоткеи.md").rename(new)
    with pytest.raises(VaultRejected, match="403"):
        await api.set_status("2026-11-11 Хоткеи", RAW, "read")
    assert "status: unread" in new.read_text(encoding="utf-8")


async def test_ambiguous_and_skip(api):
    a = await api.save("A", md(), DAY)
    b = await api.save("B", md(), DAY)
    with pytest.raises(NoteAmbiguous, match="несколько"):
        await api.set_status("нет такой", CANON, "read")
    assert await api.set_status("нет такой", CANON, "read", skip={a}) == b


async def test_not_found(api):
    with pytest.raises(NoteNotFound):
        await api.set_status("нет", "https://example.com/x", "read")
    with pytest.raises(NoteNotFound):
        await api.delete("нет", "https://example.com/x")


async def test_replace_and_noop(api, srv):
    name = await api.save("Хоткеи", md(), DAY)
    assert await api.replace(name, CANON, md(body="подробно")) == name
    assert "подробно" in notes(srv)[f"{FOLDER}/{name}.md"]
    writes = len(srv.writes)
    await api.replace(name, CANON, md(body="подробно"))
    assert len(srv.writes) == writes


async def test_replace_conflict_rereads_and_retries(api, srv):
    name = await api.save("Хоткеи", md(), DAY)
    srv.fail("409", method="PUT")
    await api.replace(name, CANON, md(body="новое"))
    assert "новое" in notes(srv)[f"{FOLDER}/{name}.md"]


async def test_delete(api, srv):
    name = await api.save("Хоткеи", md(), DAY)
    await api.delete(name, CANON)
    assert notes(srv) == {}


async def test_unread_sorted_with_titles(api):
    await api.save("Новая", md(title="Новая", url="https://a.com/1", added="2026-11-10"), DAY)
    await api.save("Старая", md(title="Старая", url="https://a.com/2", added="2026-11-01"), DAY)
    await api.save("Прочит", md(url="https://a.com/3", status="read"), DAY)
    got = await api.unread()
    assert [(n.title, n.date_added, n.read_minutes) for n in got] == [
        ("⌨️ Старая", date(2026, 11, 1), 3),
        ("⌨️ Новая", date(2026, 11, 10), 3),
    ]


async def test_find_by_url(api):
    assert await api.find_by_url(RAW) == []
    name = await api.save("Хоткеи", md(), DAY)
    assert await api.find_by_url(RAW) == [name]


@pytest.mark.parametrize("kind", ["500", "503", "429"])
async def test_retryable_errors_are_unavailable(api, srv, kind):
    srv.fail(kind, retry_after=42)
    with pytest.raises(VaultUnavailable) as err:
        await api.save("Хоткеи", md(), DAY)
    if kind != "500":
        assert err.value.retry_after == 42


@pytest.mark.parametrize("kind", ["401", "403", "422"])
async def test_permanent_errors_are_rejected(api, srv, kind):
    srv.fail(kind)
    with pytest.raises(VaultRejected, match=kind):
        await api.save("Хоткеи", md(), DAY)


async def test_bad_tags_rejected_by_site(api):
    with pytest.raises(VaultRejected, match="422"):
        await api.save("Хоткеи", md().replace("[dev]", "[hacking]"), DAY)


async def test_server_down_is_unavailable(api, srv):
    await srv.stop()
    with pytest.raises(VaultUnavailable, match="недоступен|не ответил"):
        await api.save("Хоткеи", md(), DAY)


async def test_timeout_is_unavailable(srv):
    srv.timeout_delay = 1.0
    srv.fail("timeout")
    api = ApiVault(srv.url, srv.token, FOLDER, timeout_s=0.2)
    with pytest.raises(VaultUnavailable, match="не ответил"):
        await api.save("Хоткеи", md(), DAY)
    await api.aclose()


async def test_drop_after_write_is_unavailable_and_retry_safe(api, srv):
    srv.fail("drop")
    key = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(VaultUnavailable):
        await api.save("Хоткеи", md(), DAY, idempotency_key=key)
    assert await api.save("Хоткеи", md(), DAY, idempotency_key=key) == "2026-11-11 Хоткеи"
    assert len(notes(srv)) == 1


async def test_wrong_token_rejected(srv):
    api = ApiVault(srv.url, "wrong", FOLDER, timeout_s=1)
    with pytest.raises(VaultRejected, match="401"):
        await api.unread()
    await api.aclose()


async def test_token_never_logged_or_in_repr(api, srv, caplog):
    caplog.set_level(logging.DEBUG)
    srv.fail("500")
    with pytest.raises(VaultUnavailable) as err:
        await api.save("Хоткеи", md(), DAY)
    await api.ready()
    assert srv.token not in caplog.text
    assert srv.token not in repr(api) and srv.token not in str(err.value)


async def test_tasks_capture_ask(api, srv):
    task = await api.create_task("лаба", due="2026-11-13", priority="high", tags=["university"])
    assert task["id"] and srv.tasks[0]["due"] == "2026-11-13"
    await api.capture("идея", "weekly")
    assert srv.captures[0]["target"] == "weekly"
    ans = await api.ask("что?")
    assert ans["answer_markdown"] == srv.ask_answer


def test_note_key_deterministic():
    k = "33333333-3333-4333-8333-333333333333"
    assert note_key(k, "sha1") == note_key(k, "sha1") != note_key(k, "sha2")
    assert note_key("не uuid", "sha") == note_key("не uuid", "sha")


async def test_mock_transport_http_date_retry_after():
    """Retry-After в виде HTTP-даты не ломает разбор (повтор по своему расписанию)."""

    def handler(request):
        return httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = ApiVault("http://sb/api/v1", "t", FOLDER, client=client)
    with pytest.raises(VaultUnavailable) as err:
        await api.unread()
    assert err.value.retry_after is None
    await client.aclose()


@pytest.mark.parametrize("body", ['{"error": "boom"}', "[1, 2]", '"text"', "<html>"])
async def test_non_contract_error_bodies_are_vault_errors(body):
    """Финальная проверка #2: тело ошибки не по контракту — всё равно VaultRejected."""

    def handler(request):
        return httpx.Response(403, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = ApiVault("http://sb/api/v1", "t", FOLDER, client=client)
    with pytest.raises(VaultRejected, match="403"):
        await api.save("x", md(), DAY)
    await client.aclose()


async def test_404_outside_note_path_is_unavailable():
    """Финальная проверка #6: 404 на POST /notes — неверный адрес, повторяем, а не dead."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    api = ApiVault("http://sb/wrong", "t", FOLDER, client=client)
    with pytest.raises(VaultUnavailable, match="адрес не найден"):
        await api.save("x", md(), DAY)
    await client.aclose()
