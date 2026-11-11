"""Фейковый Second Brain сам соответствует контракту — иначе тесты бота ничего не доказывают."""

import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from tests.fakes.second_brain import FOLDER, FakeSecondBrain

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def md(url=URL, status="unread", tags="[dev]", body="Текст"):
    return (
        f"---\ntype: digest\nstatus: {status}\nsource: youtube\nurl: {url}\n"
        f"date_added: 2026-11-11\nread_minutes: 2\ntags: {tags}\n---\n# ⌨️ Хоткеи\n\n{body}\n"
    )


@pytest.fixture
async def srv(tmp_path):
    async with FakeSecondBrain(tmp_path / "vault") as s:
        yield s


@pytest.fixture
async def c(srv):
    headers = {"Authorization": f"Bearer {srv.token}"}
    async with httpx.AsyncClient(base_url=srv.url, headers=headers, timeout=2) as client:
        yield client


def key(n: int) -> dict[str, str]:
    return {"Idempotency-Key": f"00000000-0000-4000-8000-{n:012d}"}


async def create(c, name="2026-11-11 Хоткеи", content=None, n=1, folder=FOLDER):
    body = {"folder": folder, "name": name, "content": content or md()}
    return await c.post("/notes", json=body, headers=key(n))


def enc(path: str) -> str:
    return quote(path, safe="/")


async def test_health_without_token(srv):
    async with httpx.AsyncClient(base_url=srv.url) as anon:
        r = await anon.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        r = await anon.get("/notes")
        assert r.status_code == 401
        assert r.json()["error"] == {
            "code": "unauthorized",
            "message": "неверный токен",
            "retryable": False,
        }


async def test_create_writes_exact_content_and_suffixes_names(c, srv):
    r1 = await create(c, n=1)
    r2 = await create(c, n=2)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["name"] == "2026-11-11 Хоткеи"
    assert r2.json()["name"] == "2026-11-11 Хоткеи (2)"
    assert srv.files()[f"{FOLDER}/2026-11-11 Хоткеи.md"] == md()
    assert r1.json()["pushed"] is True and r1.json()["sha"]


async def test_idempotent_replay_and_mismatch(c, srv):
    r1 = await create(c, n=7)
    r2 = await create(c, n=7)
    assert r1.json() == r2.json() and len(srv.files()) == 1
    r3 = await create(c, name="другое", n=7)
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "idempotency_mismatch"
    assert len(srv.writes) == 1


async def test_create_validation(c):
    assert (await create(c, folder="01 - Projects")).status_code == 403
    assert (await create(c, content="без frontmatter", n=2)).status_code == 422
    assert (await create(c, content=md(tags="[dev, hacking]"), n=3)).status_code == 422
    assert (await create(c, content=md(tags="[fun]"), n=4)).status_code == 201
    assert (await create(c, name="a/b", n=5)).status_code == 422


async def test_lookup_uses_contract_normalize_cases(c):
    await create(c)
    cases = json.loads((ROOT / "contract/url_normalize_cases.json").read_text(encoding="utf-8"))
    yt = [x["url"] for x in cases["cases"] if x["key"] == "youtube:dQw4w9WgXcQ"]
    assert len(yt) >= 10
    for url in yt:
        items = (await c.get("/notes/lookup", params={"url": url})).json()["items"]
        assert [i["name"] for i in items] == ["2026-11-11 Хоткеи"], url
    none = await c.get("/notes/lookup", params={"url": "https://example.com/x"})
    assert none.status_code == 200 and none.json() == {"items": []}


async def test_lookup_finds_moved_note(c, srv):
    await create(c)
    moved = srv.root / "05 - Archive" / "мой.md"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(md().encode())
    items = (await c.get("/notes/lookup", params={"url": "https://youtu.be/dQw4w9WgXcQ"})).json()
    assert sorted(i["name"] for i in items["items"]) == ["2026-11-11 Хоткеи", "мой"]


async def test_list_filters_and_sorts(c):
    await create(c, name="2026-11-02 B", content=md(url="https://b.com", status="unread"), n=1)
    await create(c, name="2026-11-01 A", content=md(url="https://a.com", status="unread"), n=2)
    await create(c, name="2026-11-03 C", content=md(url="https://c.com", status="read"), n=3)
    r = await c.get(
        "/notes", params={"folder": FOLDER, "fm.status": "unread", "sort": "fm.date_added"}
    )
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["2026-11-01 A", "2026-11-02 B"]
    assert items[0]["title"] == "⌨️ Хоткеи"
    assert items[0]["frontmatter"]["tags"] == ["dev"]


async def test_patch_frontmatter_only_changes_key(c, srv):
    content = md(body="status: unread — в теле не трогать")
    path = (await create(c, content=content)).json()["path"]
    r = await c.patch(f"/notes/{enc(path)}/frontmatter", json={"set": {"status": "read"}},
                      headers=key(2))  # fmt: skip
    assert r.status_code == 200
    assert srv.files()[path] == content.replace("status: unread\n", "status: read\n", 1)


async def test_put_requires_matching_sha(c, srv):
    created = (await create(c)).json()
    path = enc(created["path"])
    stale = await c.put(f"/notes/{path}", json={"content": md(body="new"), "if_sha": "bad"},
                        headers=key(2))  # fmt: skip
    assert stale.status_code == 409 and stale.json()["current_sha"] == created["sha"]
    ok = await c.put(f"/notes/{path}", json={"content": md(body="new"), "if_sha": created["sha"]},
                     headers=key(3))  # fmt: skip
    assert ok.status_code == 200 and "new" in srv.files()[created["path"]]


async def test_get_and_delete(c, srv):
    path = (await create(c)).json()["path"]
    got = (await c.get(f"/notes/{enc(path)}")).json()
    assert got["name"] == "2026-11-11 Хоткеи" and got["frontmatter"]["status"] == "unread"
    assert (await c.delete(f"/notes/{enc(path)}", headers=key(2))).status_code == 204
    assert (await c.delete(f"/notes/{enc(path)}", headers=key(2))).status_code == 204  # повтор
    assert (await c.get(f"/notes/{enc(path)}")).status_code == 404
    assert srv.files() == {}


async def test_delete_outside_delete_paths_forbidden(c, srv):
    inbox = srv.root / "00 - Inbox" / "x.md"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("---\na: b\n---\n", encoding="utf-8")
    r = await c.delete(f"/notes/{enc('00 - Inbox/x.md')}", headers=key(1))
    assert r.status_code == 403 and inbox.exists()


async def test_path_traversal_rejected(c):
    r = await c.get("/notes/..%2F..%2Fsecret.md")
    assert r.status_code in (400, 404)


async def test_capture_and_tasks(c, srv):
    r = await c.post("/capture", json={"text": "идея", "target": "inbox"}, headers=key(1))
    assert r.status_code == 201 and r.json()["path"].startswith("00 - Inbox/")
    r = await c.post("/capture", json={"text": "итог", "target": "weekly"}, headers=key(2))
    assert r.json()["path"].startswith("02 - Weekly/")
    bad = await c.post("/tasks", json={"text": "x", "tags": ["hacking"]}, headers=key(3))
    assert bad.status_code == 422
    t = await c.post("/tasks", json={"text": "лаба", "due": "2026-11-13", "priority": "high",
                                     "tags": ["university"]}, headers=key(4))  # fmt: skip
    assert t.status_code == 201 and t.json()["status"] == "open"
    tid = t.json()["id"]
    assert (await c.patch(f"/tasks/{tid}", json={"status": "done"}, headers=key(5))).json()[
        "status"
    ] == "done"
    assert (await c.delete(f"/tasks/{tid}", headers=key(6))).status_code == 204
    assert srv.tasks == []


async def test_ask(c, srv):
    await create(c)
    r = (await c.post("/ask", json={"question": "что?"})).json()
    assert r["answer_markdown"] == srv.ask_answer
    assert r["sources"][0]["name"] == "2026-11-11 Хоткеи"


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("500", 500, "internal_error"),
        ("503", 503, "vault_unavailable"),
        ("429", 429, "rate_limited"),
        ("403", 403, "forbidden"),
        ("409", 409, "conflict"),
        ("422", 422, "validation_error"),
    ],
)
async def test_fault_injection(c, srv, kind, status, code):
    srv.fail(kind, retry_after=5)
    r = await create(c)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert srv.files() == {}
    if kind in ("503", "429"):
        assert r.headers["Retry-After"] == "5"
    assert (await create(c)).status_code == 201  # сбой — один раз


async def test_fault_filter_by_method_and_path(c, srv):
    srv.fail("500", times=2, method="POST", path="/tasks")
    assert (await create(c)).status_code == 201
    assert (await c.post("/tasks", json={"text": "a"}, headers=key(9))).status_code == 500


async def test_timeout_fault_writes_late(c, srv):
    srv.timeout_delay = 0.5
    srv.fail("timeout")
    with pytest.raises(httpx.ReadTimeout):
        await c.post("/notes", json={"folder": FOLDER, "name": "x", "content": md()},
                     headers=key(1), timeout=0.1)  # fmt: skip
    assert (await create(c, name="x", n=1)).status_code == 201  # повтор с ключом — без дубля
    assert len(srv.files()) == 1


async def test_drop_after_write(c, srv):
    srv.fail("drop")
    with pytest.raises(httpx.TransportError):
        await create(c)
    assert len(srv.files()) == 1  # записал, но ответ потерялся
    again = await create(c)  # тот же ключ
    assert again.status_code == 201 and len(srv.files()) == 1


async def test_stop_start_same_port(srv, c):
    port = srv.port
    await srv.stop()
    with pytest.raises(httpx.TransportError):  # Windows: ConnectTimeout, Linux: ConnectError
        await c.get("/health", timeout=0.5)
    await srv.start()
    assert srv.port == port
    assert (await c.get("/health")).status_code == 200


async def test_sibling_folder_is_not_inside_rights(c):
    """Ревью A+B: «…Из ссылок2» начинается с имени папки бота, но в правах её нет."""
    r = await create(c, folder=FOLDER + "2")
    assert r.status_code == 403


async def test_put_and_patch_check_write_rights(c, srv):
    other = srv.root / "05 - Archive" / "x.md"
    other.parent.mkdir(parents=True)
    other.write_bytes(md().encode())
    path = enc("05 - Archive/x.md")
    sha = (await c.get(f"/notes/{path}")).json()["sha"]
    r = await c.put(f"/notes/{path}", json={"content": md(body="new"), "if_sha": sha},
                    headers=key(1))  # fmt: skip
    assert r.status_code == 403
    r = await c.patch(f"/notes/{path}/frontmatter", json={"set": {"status": "read"}},
                      headers=key(2))  # fmt: skip
    assert r.status_code == 403 and other.read_bytes() == md().encode()


@pytest.mark.parametrize(
    "bad", ["..%5C..%5C00 - Inbox%5Cx.md", "sub/.git/config", "a/.obsidian/x.md", "C:%5Cx.md"]
)
async def test_backslash_and_hidden_paths_rejected(c, srv, bad):
    inbox = srv.root / "00 - Inbox" / "x.md"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("---\na: b\n---\n", encoding="utf-8")
    r = await c.delete(f"/notes/{enc(FOLDER)}/{bad}", headers=key(1))
    assert r.status_code == 400 and inbox.exists()


async def test_bad_json_is_400(c):
    r = await c.post("/tasks", content=b"{not json", headers=key(1))
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
    r = await c.post("/ask", content=b"{not json")
    assert r.status_code == 400


async def test_fingerprint_ignores_key_order(c, srv):
    body = '{"text": "x", "target": "inbox"}'
    swapped = '{"target": "inbox", "text": "x"}'
    h = {**key(1), "Content-Type": "application/json"}
    assert (await c.post("/capture", content=body, headers=h)).status_code == 201
    assert (await c.post("/capture", content=swapped, headers=h)).status_code == 201
    assert len(srv.captures) == 1


async def test_patch_no_change_no_commit_and_validated(c, srv):
    created = (await create(c)).json()
    path = enc(created["path"])
    r = await c.patch(f"/notes/{path}/frontmatter", json={"set": {"status": "unread"}},
                      headers=key(2))  # fmt: skip
    assert r.status_code == 200 and len(srv.writes) == 1
    r = await c.patch(f"/notes/{path}/frontmatter", json={"set": {"status": "bogus"}},
                      headers=key(3))  # fmt: skip
    assert r.status_code == 422


async def test_live_check_script_detects_broken_site(srv, monkeypatch, capsys):
    """Скрипт живой проверки действительно ловит нарушения: сайт без проверки тегов → ❌."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_api", ROOT / "scripts/check_second_brain_api.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("SECOND_BRAIN_API_URL", srv.url)
    monkeypatch.setenv("SECOND_BRAIN_API_TOKEN", srv.token)
    srv.allowed_tags = srv.allowed_tags | {"hacking"}
    assert await mod.run() == 1
    assert "❌ 422 на недопустимый тег" in capsys.readouterr().out
