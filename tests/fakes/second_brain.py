"""Фейковый сайт Second Brain: настоящий HTTP-сервер (aiohttp) на localhost по контракту v1.

Эталон поведения из docs/SECOND_BRAIN_API_CONTRACT.md: токен и права по папкам, Idempotency-Key,
поиск по нормализованному url, фильтр по frontmatter, задачи, захват, /ask. Заметки — файлы во
временной папке (не git: коммиты имитируются счётчиком).

Инъекция сбоев для проверки офлайн-очереди бота:
    srv.fail("503", times=2, retry_after=7)   # 500 | 503 | 429 | 401 | 403 | 409 | 422
    srv.fail("timeout")                       # ответить позже таймаута клиента (запись — после)
    srv.fail("drop")                          # выполнить запрос, но оборвать соединение до ответа
    await srv.stop(); await srv.start()       # «сервер выключен» — тот же порт
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web

from digest.classifier import normalize
from digest.vault import frontmatter

ROOT = Path(__file__).resolve().parents[2]
FOLDER = "03 - Resources/📥 Из ссылок"
INBOX = "00 - Inbox"
WEEKLY = "02 - Weekly"
TASKS_NOTE = "01 - Tasks/Задачи.md"
PREFIX = "/api/v1"
_FM = re.compile(r"\A---\n(.*?\n)---\n", re.S)


def _allowed_tags() -> set[str]:
    data = json.loads((ROOT / "contract/allowed_tags.json").read_text(encoding="utf-8"))
    return {t["tag"] for t in data["tags"]}


@dataclass
class Fault:
    kind: str
    times: int
    method: str | None
    path: str | None
    retry_after: float | None


def _err(status: int, code: str, message: str, *, retryable: bool, **extra) -> web.Response:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "retryable": retryable}}
    body.update(extra)
    return web.json_response(body, status=status)


def _sha(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def _tags(raw: str) -> list[str]:
    raw = raw.strip().strip("[]")
    return [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]


def _fm_out(content: str) -> dict[str, Any]:
    fm: dict[str, Any] = dict(frontmatter(content) or {})
    if "tags" in fm:
        fm["tags"] = _tags(fm["tags"])
    return fm


def _title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _set_fm(content: str, values: dict[str, str]) -> str:
    m = _FM.match(content)
    if m is None:
        raise ValueError("нет frontmatter")
    block = m.group(1)
    for key, value in values.items():
        line = f"{key}: {value}"
        if re.search(rf"(?m)^{re.escape(key)}:", block):
            block = re.sub(rf"(?m)^{re.escape(key)}:.*$", lambda _, x=line: x, block, count=1)
        else:
            block += line + "\n"
    return f"---\n{block}---\n" + content[m.end() :]


class FakeSecondBrain:
    def __init__(
        self,
        root: Path,
        *,
        token: str = "sb-test-token-5f2a",
        write_paths: tuple[str, ...] = (FOLDER, INBOX, WEEKLY, TASKS_NOTE),
        delete_paths: tuple[str, ...] = (FOLDER,),
        ai: bool = True,
        port: int = 0,
        timeout_delay: float = 3.0,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.write_paths = write_paths
        self.delete_paths = delete_paths
        self.ai = ai
        self.port = port
        self.timeout_delay = timeout_delay
        self.allowed_tags = _allowed_tags()
        self.tasks: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []  # выполненные изменения (не повторы по ключу)
        self.requests: list[dict[str, Any]] = []  # все запросы, включая сбойные
        self.ask_answer = "**Docker** — контейнеры. См. заметки."
        self._idem: dict[str, tuple[str, int, Any]] = {}
        self._inflight: set[str] = set()
        self._faults: list[Fault] = []
        self._commit = 0
        self._task_seq = 0
        self._runner: web.AppRunner | None = None

    # --- управление ---------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{PREFIX}"

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application(middlewares=[self._middleware], client_max_size=20 * 1024 * 1024)
        r = app.router
        r.add_get(f"{PREFIX}/health", self._health)
        r.add_post(f"{PREFIX}/notes", self._create)
        r.add_get(f"{PREFIX}/notes/lookup", self._lookup)
        r.add_get(f"{PREFIX}/notes", self._list)
        r.add_patch(f"{PREFIX}/notes/{{path:.+}}/frontmatter", self._patch_fm)
        r.add_get(f"{PREFIX}/notes/{{path:.+}}", self._get)
        r.add_put(f"{PREFIX}/notes/{{path:.+}}", self._put)
        r.add_delete(f"{PREFIX}/notes/{{path:.+}}", self._delete)
        r.add_post(f"{PREFIX}/capture", self._capture)
        r.add_get(f"{PREFIX}/tasks", self._tasks_list)
        r.add_post(f"{PREFIX}/tasks", self._task_create)
        r.add_patch(f"{PREFIX}/tasks/{{id}}", self._task_patch)
        r.add_delete(f"{PREFIX}/tasks/{{id}}", self._task_delete)
        r.add_post(f"{PREFIX}/ask", self._ask)
        runner = web.AppRunner(app, shutdown_timeout=0.2, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        if not self.port:
            self.port = runner.addresses[0][1]
        self._runner = runner

    async def stop(self) -> None:
        """«Сервер выключен»: порт закрыт, соединения рвутся; данные остаются."""
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    async def __aenter__(self) -> "FakeSecondBrain":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def fail(
        self,
        kind: str,
        times: int = 1,
        *,
        method: str | None = None,
        path: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Следующие `times` подходящих запросов (метод и начало пути после /api/v1) — сбой."""
        self._faults.append(Fault(kind, times, method, path, retry_after))

    def files(self) -> dict[str, str]:
        out = {}
        for p in sorted(self.root.rglob("*.md")):
            out[p.relative_to(self.root).as_posix()] = p.read_text(encoding="utf-8")
        return out

    # --- инфраструктура -------------------------------------------------------------------

    def _take_fault(self, request: web.Request) -> Fault | None:
        rel = request.path.removeprefix(PREFIX)
        for f in self._faults:
            if f.method and f.method != request.method:
                continue
            if f.path and not rel.startswith(f.path):
                continue
            f.times -= 1
            if f.times <= 0:
                self._faults.remove(f)
            return f
        return None

    @web.middleware
    async def _middleware(self, request: web.Request, handler) -> web.StreamResponse:
        self.requests.append({"method": request.method, "path": request.path})
        fault = self._take_fault(request)
        if fault is not None and fault.kind not in ("timeout", "drop"):
            return self._fault_response(fault)
        if fault is not None and fault.kind == "timeout":
            await asyncio.sleep(self.timeout_delay)
        if request.path != f"{PREFIX}/health":
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                return _err(401, "unauthorized", "неверный токен", retryable=False)
        try:
            resp = await self._idempotent(request, handler)
        except json.JSONDecodeError:
            return _err(400, "validation_error", "тело — не JSON", retryable=False)
        if fault is not None and fault.kind == "drop" and request.transport is not None:
            request.transport.abort()  # записали, но ответ до клиента не дойдёт
        return resp

    def _fault_response(self, f: Fault) -> web.Response:
        headers = {"Retry-After": str(int(f.retry_after))} if f.retry_after is not None else {}
        table = {
            "500": (500, "internal_error", True),
            "503": (503, "vault_unavailable", True),
            "429": (429, "rate_limited", True),
            "401": (401, "unauthorized", False),
            "403": (403, "forbidden", False),
            "409": (409, "conflict", True),
            "422": (422, "validation_error", False),
        }
        status, code, retryable = table[f.kind]
        resp = _err(status, code, f"сбой {f.kind} (инъекция)", retryable=retryable)
        resp.headers.update(headers)
        return resp

    async def _idempotent(self, request: web.Request, handler) -> web.StreamResponse:
        key = request.headers.get("Idempotency-Key")
        if request.method == "GET" or not key:
            return await handler(request)
        body = await request.read()
        # Отпечаток по смыслу, а не по байтам (контракт): метод, раскодированный путь,
        # отсортированный query, разобранный JSON с отсортированными ключами.
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            return _err(400, "validation_error", "тело — не JSON", retryable=False)
        canon = json.dumps(
            [request.method, request.path, sorted(request.query.items()), parsed],
            sort_keys=True,
            ensure_ascii=False,
        )
        fp = hashlib.sha256(canon.encode()).hexdigest()
        if key in self._idem:
            stored_fp, status, payload = self._idem[key]
            if stored_fp != fp:
                return _err(409, "idempotency_mismatch", "ключ с другим телом", retryable=False)
            if payload is None:
                return web.Response(status=status)
            return web.json_response(payload, status=status)
        if key in self._inflight:
            return _err(409, "in_progress", "запрос с этим ключом ещё выполняется", retryable=True)
        self._inflight.add(key)
        try:
            resp = await handler(request)
        finally:
            self._inflight.discard(key)
        if resp.status < 500 and isinstance(resp, web.Response):
            payload = json.loads(resp.text or "null") if resp.body else None
            self._idem[key] = (fp, resp.status, payload)
        return resp

    def _file(self, rel: str) -> Path:
        p = PurePosixPath(rel)
        bad = {"..", ".git", ".obsidian"}
        if "\\" in rel or p.is_absolute() or bad & set(p.parts) or ":" in rel:
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {"error": {"code": "validation_error", "message": "путь", "retryable": False}}
                ),  # fmt: skip
                content_type="application/json",
            )
        return self.root / Path(*p.parts)

    @staticmethod
    def _within(rel: str, roots: tuple[str, ...]) -> bool:
        return any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in roots)

    def _commit_id(self) -> str:
        self._commit += 1
        return f"{self._commit:07x}"

    def _log_write(self, request: web.Request, note: str | None = None) -> None:
        rel = request.path.removeprefix(PREFIX)
        route = rel if not rel.startswith("/notes/") else "/notes/{path}"
        self.writes.append({"method": request.method, "path": route, "note": note})

    def _check_content(self, content: str) -> str | None:
        fm = frontmatter(content)
        if fm is None:
            return "нет frontmatter"
        if fm.get("type") == "digest":
            if not fm.get("url"):
                return "нет url"
            if fm.get("status") not in ("unread", "read"):
                return "status должен быть unread или read"
            tags = _tags(fm.get("tags", ""))
            if len(tags) > 3 or not set(tags) <= self.allowed_tags:
                return f"недопустимые теги: {tags}"
        return None

    def _note_json(self, rel: str, content: str) -> dict[str, Any]:
        return {
            "path": rel,
            "name": PurePosixPath(rel).stem,
            "content": content,
            "frontmatter": _fm_out(content),
            "sha": _sha(content),
        }

    # --- заметки --------------------------------------------------------------------------

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "vault_synced_at": datetime.now(UTC).isoformat(), "version": "fake"}
        )

    async def _create(self, request: web.Request) -> web.Response:
        data = await request.json()
        folder, name, content = data.get("folder", ""), data.get("name", ""), data.get("content")
        if not isinstance(content, str) or not name or re.search(r'[\\/:*?"<>|#^\[\]]', name):
            return _err(422, "validation_error", "имя или содержимое", retryable=False)
        if not self._within(folder, self.write_paths):
            return _err(403, "forbidden", f"папка {folder} вне прав", retryable=False)
        if problem := self._check_content(content):
            return _err(422, "validation_error", problem, retryable=False)
        base = f"{folder}/{name}"
        rel, i = f"{base}.md", 2
        while self._file(rel).exists():
            rel, i = f"{base} ({i}).md", i + 1
        path = self._file(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        self._log_write(request, rel)
        return web.json_response(
            {
                "path": rel,
                "name": PurePosixPath(rel).stem,
                "sha": _sha(content),
                "commit": self._commit_id(),
                "pushed": True,
            },
            status=201,
        )

    async def _lookup(self, request: web.Request) -> web.Response:
        key = normalize(request.query.get("url", ""))
        items = []
        for rel, content in self.files().items():
            fm = frontmatter(content) or {}
            if fm.get("url") and normalize(fm["url"]) == key:
                items.append(
                    {"path": rel, "name": PurePosixPath(rel).stem, "frontmatter": _fm_out(content)}
                )
        return web.json_response({"items": items})

    async def _list(self, request: web.Request) -> web.Response:
        folder = request.query.get("folder", "")
        filters = {k[3:]: v for k, v in request.query.items() if k.startswith("fm.")}
        items = []
        for rel, content in self.files().items():
            if folder and not self._within(rel, (folder,)):
                continue
            fm = frontmatter(content) or {}
            if any(fm.get(k) != v for k, v in filters.items()):
                continue
            name = PurePosixPath(rel).stem
            items.append(
                {
                    "path": rel,
                    "name": name,
                    "title": _title(content, name),
                    "frontmatter": _fm_out(content),
                }  # fmt: skip
            )
        sort = request.query.get("sort", "")
        if sort:
            field = sort.lstrip("-").removeprefix("fm.")
            items.sort(
                key=lambda it: (str(it["frontmatter"].get(field, "")), it["name"]),
                reverse=sort.startswith("-"),
            )
        limit = min(int(request.query.get("limit", "100")), 500)
        return web.json_response({"items": items[:limit]})

    def _existing(self, request: web.Request) -> tuple[str, Path] | web.Response:
        rel = request.match_info["path"]
        path = self._file(rel)
        if not path.is_file():
            return _err(404, "not_found", f"нет заметки {rel}", retryable=False)
        return rel, path

    def _sha_conflict(self, if_sha: str | None, content: str) -> web.Response | None:
        if if_sha and if_sha != _sha(content):
            return _err(409, "conflict", "sha не совпал", retryable=True, current_sha=_sha(content))
        return None

    async def _get(self, request: web.Request) -> web.Response:
        found = self._existing(request)
        if isinstance(found, web.Response):
            return found
        rel, path = found
        return web.json_response(self._note_json(rel, path.read_text(encoding="utf-8")))

    async def _patch_fm(self, request: web.Request) -> web.Response:
        found = self._existing(request)
        if isinstance(found, web.Response):
            return found
        rel, path = found
        if not self._within(rel, self.write_paths):
            return _err(403, "forbidden", "вне прав", retryable=False)
        data = await request.json()
        content = path.read_text(encoding="utf-8")
        if conflict := self._sha_conflict(data.get("if_sha"), content):
            return conflict
        new = _set_fm(content, {k: str(v) for k, v in (data.get("set") or {}).items()})
        if problem := self._check_content(new):
            return _err(422, "validation_error", problem, retryable=False)
        if new == content:  # ничего не изменилось — без нового коммита
            return web.json_response(
                {"path": rel, "sha": _sha(new), "commit": f"{self._commit:07x}", "pushed": True}
            )
        if new != content:
            path.write_bytes(new.encode("utf-8"))
            self._log_write(request, rel)
        return web.json_response(
            {"path": rel, "sha": _sha(new), "commit": self._commit_id(), "pushed": True}
        )

    async def _put(self, request: web.Request) -> web.Response:
        found = self._existing(request)
        if isinstance(found, web.Response):
            return found
        rel, path = found
        if not self._within(rel, self.write_paths):
            return _err(403, "forbidden", "вне прав", retryable=False)
        data = await request.json()
        content = path.read_text(encoding="utf-8")
        if not data.get("if_sha"):
            return _err(422, "validation_error", "if_sha обязателен", retryable=False)
        if conflict := self._sha_conflict(data["if_sha"], content):
            return conflict
        new = data.get("content")
        if not isinstance(new, str) or self._check_content(new):
            return _err(422, "validation_error", "содержимое", retryable=False)
        path.write_bytes(new.encode("utf-8"))
        self._log_write(request, rel)
        return web.json_response(
            {"path": rel, "sha": _sha(new), "commit": self._commit_id(), "pushed": True}
        )

    async def _delete(self, request: web.Request) -> web.StreamResponse:
        found = self._existing(request)
        if isinstance(found, web.Response):
            return found
        rel, path = found
        if not self._within(rel, self.delete_paths):
            return _err(403, "forbidden", "удалять здесь нельзя", retryable=False)
        if conflict := self._sha_conflict(request.query.get("if_sha"), path.read_text("utf-8")):
            return conflict
        path.unlink()
        self._log_write(request, rel)
        return web.Response(status=204)

    # --- захват, задачи, ИИ -----------------------------------------------------------

    async def _capture(self, request: web.Request) -> web.Response:
        data = await request.json()
        text, target = (data.get("text") or "").strip(), data.get("target")
        if not text or target not in ("inbox", "weekly"):
            return _err(422, "validation_error", "text/target", retryable=False)
        today = date.today()
        if target == "inbox":
            rel = f"{INBOX}/{today.isoformat()}.md"
        else:
            year, week, _ = today.isocalendar()
            rel = f"{WEEKLY}/{year}-W{week:02d}.md"
        if not self._within(rel, self.write_paths):
            return _err(403, "forbidden", "вне прав", retryable=False)
        path = self._file(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_bytes((old + f"- {text}\n").encode("utf-8"))
        self.captures.append({"text": text, "target": target, "source": data.get("source")})
        self._log_write(request, rel)
        name = PurePosixPath(rel).stem
        return web.json_response(
            {"path": rel, "name": name, "commit": self._commit_id(), "pushed": True}, status=201
        )

    async def _tasks_list(self, request: web.Request) -> web.Response:
        status = request.query.get("status")
        items = [t for t in self.tasks if not status or t["status"] == status]
        return web.json_response({"items": items})

    async def _task_create(self, request: web.Request) -> web.Response:
        if not self._within(TASKS_NOTE, self.write_paths):
            return _err(403, "forbidden", "заметка задач вне прав", retryable=False)
        data = await request.json()
        text = (data.get("text") or "").strip()
        tags = data.get("tags") or []
        priority = data.get("priority") or "normal"
        if not text or priority not in ("high", "normal", "low"):
            return _err(422, "validation_error", "text/priority", retryable=False)
        if not set(tags) <= self.allowed_tags:
            return _err(422, "validation_error", f"теги {tags}", retryable=False)
        if data.get("due"):
            try:
                date.fromisoformat(data["due"])
            except ValueError:
                return _err(422, "validation_error", "due", retryable=False)
        self._task_seq += 1
        task = {
            "id": f"t_{self._task_seq}",
            "text": text,
            "status": "open",
            "due": data.get("due"),
            "priority": priority,
            "tags": tags,
            "path": TASKS_NOTE,
        }
        self.tasks.append(task)
        self._log_write(request, TASKS_NOTE)
        return web.json_response(task, status=201)

    def _task(self, request: web.Request) -> dict[str, Any] | None:
        return next((t for t in self.tasks if t["id"] == request.match_info["id"]), None)

    async def _task_patch(self, request: web.Request) -> web.Response:
        task = self._task(request)
        if task is None:
            return _err(404, "not_found", "нет задачи", retryable=False)
        data = await request.json()
        task.update({k: v for k, v in data.items() if k in ("status", "due", "priority", "text")})
        self._log_write(request, TASKS_NOTE)
        return web.json_response(task)

    async def _task_delete(self, request: web.Request) -> web.StreamResponse:
        task = self._task(request)
        if task is None:
            return _err(404, "not_found", "нет задачи", retryable=False)
        self.tasks.remove(task)
        self._log_write(request, TASKS_NOTE)
        return web.Response(status=204)

    async def _ask(self, request: web.Request) -> web.Response:
        if not self.ai:
            return _err(403, "forbidden", "нет права ai", retryable=False)
        data = await request.json()
        if not (data.get("question") or "").strip():
            return _err(422, "validation_error", "question", retryable=False)
        sources = [{"path": rel, "name": PurePosixPath(rel).stem} for rel in list(self.files())[:2]]
        return web.json_response(
            {"answer_markdown": self.ask_answer, "sources": sources, "model": "fake-model"}
        )
