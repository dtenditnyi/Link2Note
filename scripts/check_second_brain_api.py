"""Живая проверка API сайта Second Brain по контракту v1 (запускает владелец, на песочнице).

    export SECOND_BRAIN_API_URL=http://secondbrain-web:8000/api/v1
    read -rs SECOND_BRAIN_API_TOKEN && export SECOND_BRAIN_API_TOKEN   # не в историю shell
    uv run python scripts/check_second_brain_api.py [--all]

Работает только в тестовой папке `03 - Resources/📥 Из ссылок/_contract-test`: создаёт заметку,
ищет по url, меняет статус, заменяет, проверяет идемпотентность и коды ошибок, удаляет за собой.
--all — ещё /tasks, /capture и /ask (создадут тестовую задачу и запись во «Входящих» — их
сайт не даёт удалить токеном бота, убрать вручную). Токен берётся только из окружения.
"""

import asyncio
import os
import sys
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from digest.errors import NoteNotFound, VaultError, VaultRejected  # noqa: E402
from digest.vault_api import ApiVault  # noqa: E402

FOLDER = "03 - Resources/📥 Из ссылок/_contract-test"
FORBIDDEN_FOLDER = "01 - Projects/_contract-test"


def _md(url: str, status: str = "unread", body: str = "Проверка контракта.", tags="[dev]") -> str:
    return (
        f"---\ntype: digest\nstatus: {status}\nsource: article\nurl: {url}\n"
        f"date_added: {date.today().isoformat()}\nread_minutes: 1\nconfidence: high\n"
        f"tags: {tags}\n---\n# 🧪 Link2Note contract test\n\n{body}\n"
    )


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def ok(self, name: str) -> None:
        self.passed += 1
        print(f"✅ {name}")

    def fail(self, name: str, why: str) -> None:
        self.failed += 1
        print(f"❌ {name}: {why}")


async def _check(report: Report, name: str, coro) -> object:
    try:
        result = await coro
    except Exception as err:  # noqa: BLE001 — любой сбой = ❌ с причиной
        report.fail(name, f"{type(err).__name__}: {err}")
        return None
    report.ok(name)
    return result


async def run(include_all: bool = False) -> int:
    base = os.environ.get("SECOND_BRAIN_API_URL", "").rstrip("/")
    token = os.environ.get("SECOND_BRAIN_API_TOKEN", "")
    if not base or not token:
        print("Нужны переменные окружения SECOND_BRAIN_API_URL и SECOND_BRAIN_API_TOKEN")
        return 2
    report = Report()
    api = ApiVault(base, token, FOLDER, timeout_s=20)
    raw = httpx.AsyncClient(base_url=base, timeout=20)
    auth = {"Authorization": f"Bearer {token}"}
    url = f"https://example.com/link2note-contract-test/{uuid.uuid4().hex[:8]}"
    key = str(uuid.uuid4())
    name: str | None = None
    print(f"Second Brain: {base}\nТестовая папка: {FOLDER}\n")
    try:
        info = await _check(report, "GET /health", api.health())
        if info is None:
            return 1

        async def unauthorized():
            bad = ApiVault(base, "wrong-" + uuid.uuid4().hex, FOLDER, timeout_s=20)
            try:
                await bad.unread()
            except VaultRejected as err:
                if "401" not in str(err):
                    raise AssertionError(f"ожидал 401, получил {err}") from err
                return
            finally:
                await bad.aclose()
            raise AssertionError("чужой токен принят")

        await _check(report, "401 с неверным токеном", unauthorized())

        created = await _check(
            report,
            "POST /notes",
            api.save("Contract test", _md(url), date.today(), idempotency_key=key),
        )
        if not isinstance(created, str):
            return 1
        name = created

        async def idempotent():
            again = await api.save("Contract test", _md(url), date.today(), idempotency_key=key)
            if again != name:
                raise AssertionError(f"повтор с тем же ключом дал другое имя: {again}")
            found = await api.find_by_url(url)
            if found != [name]:
                raise AssertionError(f"после повтора заметок: {found}")

        await _check(report, "Idempotency-Key: повтор без дубля", idempotent())

        async def mismatch():
            try:
                await api.save("Contract other", _md(url), date.today(), idempotency_key=key)
            except VaultRejected as err:
                if "idempotency_mismatch" not in str(err):
                    raise AssertionError(f"ожидал 409 idempotency_mismatch: {err}") from err
                return
            raise AssertionError("тот же ключ с другим телом принят")

        await _check(report, "409 idempotency_mismatch", mismatch())

        async def lookup_variants():
            for variant in (url + "?utm_source=tg", url + "/", url + "#x"):
                if await api.find_by_url(variant) != [name]:
                    raise AssertionError(f"lookup не нашёл по {variant}")

        await _check(report, "GET /notes/lookup (нормализация url)", lookup_variants())

        path = f"{FOLDER}/{name}.md"

        async def read_status():
            await api.set_status(name, url, "read")
            note = (await raw.get("/notes/" + quote(path, safe="/"), headers=auth)).json()
            if note["frontmatter"].get("status") != "read":
                raise AssertionError("status не изменился")
            if note["content"] != _md(url).replace("status: unread", "status: read", 1):
                raise AssertionError("PATCH изменил не только status")

        await _check(report, "PATCH frontmatter (только status)", read_status())

        async def unread_list():
            names = [n.name for n in await api.unread()]
            if name in names:
                raise AssertionError("прочитанная заметка в списке unread")

        await _check(report, "GET /notes?fm.status=unread", unread_list())

        new_md = _md(url, body="Подробный текст.")

        async def replace():
            await api.replace(name, url, new_md)
            note = (await raw.get("/notes/" + quote(path, safe="/"), headers=auth)).json()
            if note["content"] != new_md:
                raise AssertionError("PUT записал не то")

        await _check(report, "PUT /notes/{path}", replace())

        async def stale_sha():
            r = await raw.put(
                "/notes/" + quote(path, safe="/"),
                json={"content": _md(url, body="stale"), "if_sha": "0" * 40},
                headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
            )
            if r.status_code != 409 or "current_sha" not in r.json():
                raise AssertionError(f"ожидал 409 conflict с current_sha, получил {r.status_code}")

        await _check(report, "409 conflict на устаревший if_sha", stale_sha())

        async def bad_tags():
            try:
                await api.save("Bad tags", _md(url + "-bad", tags="[hacking]"), date.today())
            except VaultRejected as err:
                if "422" not in str(err):
                    raise AssertionError(f"ожидал 422: {err}") from err
                return
            raise AssertionError("недопустимый тег принят (заметку «Bad tags» удали вручную)")

        await _check(report, "422 на недопустимый тег", bad_tags())

        async def forbidden():
            outside = ApiVault(base, token, FORBIDDEN_FOLDER, timeout_s=20)
            try:
                await outside.save("Forbidden", _md(url + "-f"), date.today())
            except VaultRejected as err:
                if "403" not in str(err):
                    raise AssertionError(f"ожидал 403: {err}") from err
                return
            finally:
                await outside.aclose()
            raise AssertionError(f"запись в {FORBIDDEN_FOLDER} разрешена — проверь права токена")

        await _check(report, "403 вне write_paths", forbidden())

        async def fun_tag():
            extra = await api.save("Fun tag", _md(url + "-fun", tags="[fun]"), date.today())
            await api.delete(extra, url + "-fun")

        await _check(report, "тег fun разрешён", fun_tag())

        async def delete():
            await api.delete(name, url)
            if await api.find_by_url(url):
                raise AssertionError("после удаления lookup ещё находит заметку")

        await _check(report, "DELETE /notes/{path}", delete())
        try:
            if not await api.find_by_url(url):
                name = None  # удалена — убирать в finally нечего
        except VaultError:
            pass

        async def gone():
            r = await raw.get("/notes/" + quote(path, safe="/"), headers=auth)
            if r.status_code != 404:
                raise AssertionError(f"ожидал 404, получил {r.status_code}")

        await _check(report, "404 на удалённую заметку", gone())

        if include_all:
            await _check(report, "POST /tasks", api.create_task("Link2Note contract test"))
            await _check(report, "POST /capture", api.capture("Link2Note contract test", "inbox"))
            await _check(report, "POST /ask", api.ask("Проверка: ответь одним словом «ок»"))
    finally:
        if name is not None:  # убрать за собой, даже если проверки упали
            try:
                await api.delete(name, url)
            except (NoteNotFound, VaultError):
                print(f"⚠️ Не смог удалить тестовую заметку «{name}» — удали вручную")
        await raw.aclose()
        await api.aclose()
    print(f"\nИтог: пройдено {report.passed}, ошибок {report.failed}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(run("--all" in sys.argv[1:])))
