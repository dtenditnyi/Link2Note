"""Хранилище через API сайта Second Brain (VAULT_BACKEND=api), docs/SECOND_BRAIN_API_CONTRACT.md.

Сайт — единственный писатель в хранилище: он проверяет запрос, пишет файл, коммитит и пушит.
Здесь только HTTP: ошибки контракта → `VaultUnavailable` (повторить позже), `VaultRejected` (не
повторять), `NoteNotFound`, `NoteAmbiguous`. Повторы и офлайн-очередь — в `digest/outbox.py`.

Каждый изменяющий запрос несёт `Idempotency-Key`. У запросов с `if_sha` ключ — uuid5(ключ, sha)
(DECISIONS D6), а если заметка уже в нужном состоянии, запрос не отправляется вовсе.
Токен живёт только в заголовке запроса: в логи, сообщения об ошибках и repr он не попадает.
"""

import logging
import uuid
from collections.abc import Collection
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from digest.errors import NoteAmbiguous, NoteNotFound, VaultRejected, VaultUnavailable
from digest.vault import frontmatter, safe_filename
from digest.vault_base import UnreadNote

log = logging.getLogger(__name__)

UNREAD_LIMIT = 500
ASK_TIMEOUT_S = 90  # ответ ИИ сайта — дольше обычной записи
_MAX_MESSAGE = 300


class _Conflict(VaultUnavailable):
    """409 conflict: заметку изменили между чтением и записью (или запрос с этим ключом ещё
    выполняется). Там, где есть if_sha, — перечитать и повторить; иначе — как «недоступен»."""


def note_key(base: str, sha: str) -> str:
    """Ключ идемпотентности записи поверх версии sha: тот же для повторов той же версии."""
    try:
        ns = uuid.UUID(base)
    except ValueError:
        ns = uuid.uuid5(uuid.NAMESPACE_URL, base)
    return str(uuid.uuid5(ns, sha))


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None  # HTTP-дата — редкость; повтор всё равно будет по своему расписанию


def _error(resp: httpx.Response) -> tuple[str, str, bool | None]:
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        err = {}
    code = str(err.get("code") or resp.status_code)
    message = str(err.get("message") or resp.reason_phrase or "")[:_MAX_MESSAGE]
    retryable = err.get("retryable")
    return code, message, retryable if isinstance(retryable, bool) else None


class ApiVault:
    def __init__(
        self,
        base_url: str,
        token: str | SecretStr,
        folder: str,
        *,
        timeout_s: float = 15,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.folder = folder.strip("/")
        self.timeout_s = timeout_s
        self._token = token if isinstance(token, SecretStr) else SecretStr(token)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), headers={"User-Agent": "Link2Note"}
        )

    def __repr__(self) -> str:
        return f"ApiVault({self.base_url!r}, folder={self.folder!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- HTTP -------------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        key: str | None = None,
        auth: bool = True,
        limit_s: float | None = None,
    ) -> httpx.Response:
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        if key:
            headers["Idempotency-Key"] = key
        limit = limit_s or self.timeout_s
        try:
            resp = await self._client.request(
                method,
                self.base_url + path,
                json=json,
                params=params,
                headers=headers,
                timeout=httpx.Timeout(limit),
            )
        except httpx.TimeoutException as err:
            raise VaultUnavailable(
                f"Сайт Second Brain не ответил за {limit:g} с ({type(err).__name__})"
            ) from err
        except httpx.TransportError as err:
            raise VaultUnavailable(f"Сайт Second Brain недоступен ({type(err).__name__})") from err
        if resp.status_code < 400:
            return resp
        code, message, retryable = _error(resp)
        text = f"{resp.status_code} {code}: {message}".strip(": ")
        log.warning("Second Brain %s %s → %s", method, path.split("?")[0], text)
        status = resp.status_code
        if status == 409 and code == "conflict":
            raise _Conflict(f"Сайт Second Brain: конфликт версий ({text})")
        if status == 404:
            raise NoteNotFound(f"На сайте нет заметки ({text})")
        if status == 429 or status >= 500 or (status == 409 and retryable):
            raise VaultUnavailable(
                f"Сайт Second Brain временно недоступен ({text})", retry_after=_retry_after(resp)
            )
        raise VaultRejected(f"Сайт Second Brain отказал ({text})")

    def _note_url(self, path: str) -> str:
        return "/notes/" + quote(path, safe="/")

    def _path(self, name: str) -> str:
        return f"{self.folder}/{name}.md"

    # --- поиск заметки ----------------------------------------------------------------------

    async def _get(self, path: str) -> dict[str, Any]:
        return (await self._request("GET", self._note_url(path))).json()

    async def _lookup(self, url: str) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/notes/lookup", params={"url": url})
        items = resp.json().get("items") or []
        return [i for i in items if (i.get("frontmatter") or {}).get("type") == "digest"]

    async def _locate(self, name: str, url: str, skip: Collection[str]) -> dict[str, Any]:
        """Своя папка по имени; иначе — единственная заметка-конспект с этим url (как GitVault)."""
        try:
            return await self._get(self._path(name))
        except NoteNotFound:
            pass
        found = [i for i in await self._lookup(url) if i.get("name") not in skip]
        if len(found) == 1:
            return await self._get(found[0]["path"])
        if found:
            names = ", ".join(f"«{i.get('name')}»" for i in found)
            raise NoteAmbiguous(
                f"Нашёл несколько заметок с этой ссылкой: {names} — не знаю, какую.",
                note_name=None,
            )
        raise NoteNotFound(
            f"Не нашёл заметку «{name}» в хранилище — удалена или ссылка в ней изменена?",
            note_name=None,
        )

    # --- VaultBackend -----------------------------------------------------------------------

    async def ready(self) -> bool:
        try:
            await self._request("GET", "/health", auth=False)
        except (VaultUnavailable, VaultRejected, NoteNotFound) as err:
            log.warning("Second Brain /health: %s", err)
            return False
        return True

    async def health(self) -> dict[str, Any]:
        return (await self._request("GET", "/health", auth=False)).json()

    async def save(
        self, title: str, markdown: str, day: date, *, idempotency_key: str | None = None
    ) -> str:
        fm = frontmatter(markdown) or {}
        source = {k: fm.get(k) for k in ("author", "duration", "model") if fm.get(k)}
        body = {
            "folder": self.folder,
            "name": f"{day:%Y-%m-%d} {safe_filename(title)}",
            "content": markdown,
            "source": {"url": fm.get("url", ""), "kind": fm.get("source", "")} | source,
        }
        key = idempotency_key or str(uuid.uuid4())
        resp = await self._request("POST", "/notes", json=body, key=key)
        return str(resp.json()["name"])

    async def set_status(
        self,
        name: str,
        url: str,
        status: str,
        *,
        skip: Collection[str] = (),
        idempotency_key: str | None = None,
    ) -> str:
        key = idempotency_key or str(uuid.uuid4())
        for attempt in range(2):
            note = await self._locate(name, url, skip)
            if (note.get("frontmatter") or {}).get("status") == status:
                return str(note["name"])
            body = {"set": {"status": status}, "if_sha": note["sha"]}
            try:
                await self._request(
                    "PATCH",
                    self._note_url(note["path"]) + "/frontmatter",
                    json=body,
                    key=note_key(key, note["sha"]),
                )
            except _Conflict as err:
                if attempt:
                    raise VaultUnavailable(f"Заметку меняют одновременно ({err})") from err
                continue
            return str(note["name"])
        raise AssertionError("unreachable")

    async def replace(
        self,
        name: str,
        url: str,
        markdown: str,
        *,
        skip: Collection[str] = (),
        idempotency_key: str | None = None,
    ) -> str:
        key = idempotency_key or str(uuid.uuid4())
        for attempt in range(2):
            note = await self._locate(name, url, skip)
            if note.get("content") == markdown:
                return str(note["name"])
            body = {"content": markdown, "if_sha": note["sha"]}
            try:
                await self._request(
                    "PUT", self._note_url(note["path"]), json=body, key=note_key(key, note["sha"])
                )
            except _Conflict as err:
                if attempt:
                    raise VaultUnavailable(f"Заметку меняют одновременно ({err})") from err
                continue
            return str(note["name"])
        raise AssertionError("unreachable")

    async def delete(
        self,
        name: str,
        url: str,
        *,
        skip: Collection[str] = (),
        idempotency_key: str | None = None,
    ) -> None:
        key = idempotency_key or str(uuid.uuid4())
        for attempt in range(2):
            note = await self._locate(name, url, skip)
            try:
                await self._request(
                    "DELETE",
                    self._note_url(note["path"]),
                    params={"if_sha": note["sha"]},
                    key=note_key(key, note["sha"]),
                )
            except NoteNotFound:
                return  # удалили между чтением и запросом — цель достигнута
            except _Conflict as err:
                if attempt:
                    raise VaultUnavailable(f"Заметку меняют одновременно ({err})") from err
                continue
            return

    async def unread(self) -> list[UnreadNote]:
        params = {
            "folder": self.folder,
            "fm.type": "digest",
            "fm.status": "unread",
            "sort": "fm.date_added",
            "limit": str(UNREAD_LIMIT),
        }
        items = (await self._request("GET", "/notes", params=params)).json().get("items") or []
        notes = []
        for item in items:
            fm = item.get("frontmatter") or {}
            try:
                added: date | None = date.fromisoformat(str(fm.get("date_added", "")))
            except ValueError:
                added = None
            try:
                minutes = int(fm.get("read_minutes", 0))
            except (TypeError, ValueError):
                minutes = 0
            name = str(item.get("name", ""))
            notes.append(UnreadNote(name, str(item.get("title") or name), added, minutes))
        notes.sort(key=lambda n: (n.date_added or date.max, n.name))
        return notes

    async def find_by_url(self, url: str) -> list[str] | None:
        return [str(i["name"]) for i in await self._lookup(url)]

    # --- задачи, захват, ИИ ---------------------------------------------------------------

    async def create_task(
        self,
        text: str,
        *,
        due: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text}
        if due:
            body["due"] = due
        if priority:
            body["priority"] = priority
        if tags:
            body["tags"] = tags
        key = idempotency_key or str(uuid.uuid4())
        return (await self._request("POST", "/tasks", json=body, key=key)).json()

    async def capture(
        self, text: str, target: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        body = {"text": text, "target": target, "source": {"kind": "telegram"}}
        key = idempotency_key or str(uuid.uuid4())
        return (await self._request("POST", "/capture", json=body, key=key)).json()

    async def ask(self, question: str) -> dict[str, Any]:
        resp = await self._request(
            "POST", "/ask", json={"question": question}, limit_s=max(ASK_TIMEOUT_S, self.timeout_s)
        )
        return resp.json()
