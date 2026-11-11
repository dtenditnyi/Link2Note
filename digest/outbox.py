"""Офлайн-очередь операций с сайтом Second Brain (outbox) и хранилище-обёртка `QueuedVault`.

Любая запись в режиме api сначала ложится в таблицу `vault_ops` (с готовым Markdown — Gemini
повторно не вызывается и ключом идемпотентности, созданным один раз), потом сразу отправляется.
- Сайт недоступен (`VaultUnavailable`: сеть, таймаут, 5xx, 429) → операция ждёт: 30 с → 1 →
  5 → 15 мин → 1 ч → далее раз в час, не раньше `Retry-After`. Пользователь видит «📮 …» и
  сообщение обновляется само, когда операция дойдёт.
- Отказ (`VaultRejected`: 400/401/403/409 mismatch/422), заметка не найдена или их несколько →
  `dead` сразу, без повторов. Старше OUTBOX_MAX_AGE или после OUTBOX_MAX_ATTEMPTS → `dead`.
  `dead` в фоне — уведомление владельцу; `/retry` возвращает в работу.
- Операции одной заметки (группа `note:<normalize(url)>`) идут строго по порядку: «Прочитано»
  или «Удалить» ждут, пока дойдёт создание (DECISIONS D5).
- Очередь в SQLite переживает перезапуск; один `asyncio.Lock` — одна операция за раз.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any

from database import Database, OpRow
from digest.classifier import normalize
from digest.errors import (
    NoteAmbiguous,
    NoteNotFound,
    VaultError,
    VaultQueued,
    VaultRejected,
    VaultUnavailable,
)
from digest.vault import frontmatter, safe_filename
from digest.vault_api import ApiVault
from digest.vault_base import UnreadNote
from handlers import keyboards as kb

log = logging.getLogger(__name__)

BACKOFF_S = (30, 60, 300, 900, 3600)
IDLE_S = 30.0  # фоновый цикл просыпается не реже

QUEUED_NOTE = "📮 Сайт недоступен, заметка в очереди — отправлю автоматически."
QUEUED_CHANGE = "📮 Сайт недоступен — изменение в очереди, отправлю автоматически."
QUEUED_TASK = "📮 Сайт недоступен — задача в очереди, отправлю автоматически."
QUEUED_CAPTURE = "📮 Сайт недоступен — запись в очереди, отправлю автоматически."


def _utc_now() -> datetime:
    return datetime.now(UTC)


def note_group(url: str, name: str = "") -> str:
    return f"note:{normalize(url)}" if url else f"note:name:{name}"


def describe(op: OpRow) -> str:
    p = op.payload
    name = p.get("name", "")
    return {
        "save": f"заметка «{name}»",
        "set_status": f"отметка «{p.get('status')}» для «{name}»",
        "replace": f"подробный конспект «{name}»",
        "delete": f"удаление «{name}»",
        "task": f"задача «{p.get('text', '')}»",
        "capture": "запись в недельную заметку"
        if p.get("target") == "weekly"
        else "запись во «Входящие»",
    }.get(op.op, op.op)


def _kind(err: Exception) -> str:
    if isinstance(err, NoteNotFound):
        return "not_found"
    if isinstance(err, NoteAmbiguous):
        return "ambiguous"
    return "rejected"


class Outbox:
    def __init__(
        self,
        db: Database,
        api: ApiVault,
        *,
        bot=None,
        owner_id: int | None = None,
        clock: Callable[[], datetime] = _utc_now,
        max_age: timedelta = timedelta(days=7),
        max_attempts: int = 30,
    ):
        self.db = db
        self.api = api
        self.bot = bot
        self.owner_id = owner_id
        self.clock = clock
        self.max_age = max_age
        self.max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._handlers: dict[str, Callable[[OpRow], Awaitable[dict[str, Any]]]] = {
            "save": self._save,
            "set_status": self._set_status,
            "replace": self._replace,
            "delete": self._delete,
            "task": self._task,
            "capture": self._capture,
        }

    def _now(self) -> datetime:
        return self.clock().astimezone(UTC)

    # --- постановка ------------------------------------------------------------------------

    def enqueue(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        grp: str,
        chat_id: int | None = None,
        msg_id: int | None = None,
    ) -> int:
        if op not in self._handlers:
            raise ValueError(f"неизвестная операция {op}")
        key = str(uuid.uuid4())
        op_id = self.db.add_op(op, grp, payload, key, self._now(), chat_id=chat_id, msg_id=msg_id)
        log.info("Outbox: +%s #%d (%s)", op, op_id, grp)
        self._wake.set()
        return op_id

    def attach(
        self, op_id: int, *, chat_id: int, msg_id: int, text: str, note_id: int | None = None
    ) -> None:
        """Когда операция дойдёт, сообщение (chat_id, msg_id) получит text (+ кнопки заметки)."""
        self.db.update_op(
            op_id,
            self._now(),
            chat_id=chat_id,
            msg_id=msg_id,
            notify={"text": text, "note_id": note_id},
        )

    # --- выполнение ------------------------------------------------------------------------

    async def attempt(self, op_id: int) -> OpRow:
        """Сразу отправить операцию (и более ранние той же группы, не дожидаясь их времени)."""
        op = self.db.get_op(op_id)
        if op is None:
            raise KeyError(op_id)
        async with self._lock:
            for item in self.db.group_pending(op.grp, op_id):
                # Перечитать: выполненный выше save мог сменить имя в payload (сайт дал « (2)»).
                fresh = self.db.get_op(item.id)
                if fresh is None or fresh.status != "pending":
                    continue
                # О более ранней операции группы пользователь не спрашивал — её dead уведомляем.
                status = await self._execute(fresh, background=fresh.id != op_id)
                if status == "pending":
                    break  # сайт недоступен — дальше по группе не идём
        row = self.db.get_op(op_id)
        assert row is not None
        return row

    async def run_due(self) -> int:
        """Отправить всё, чьё время пришло. Возвращает число завершённых (done или dead)."""
        finished = 0
        async with self._lock:
            now = self._now()
            for op in self.db.list_ops(("pending",)):
                if now - op.started_at > self.max_age:
                    await self._dead(op, "не отправилась за OUTBOX_MAX_AGE", "expired", True)
                    finished += 1
            while due := self.db.due_ops(self._now()):
                progressed = False
                for op in due:
                    current = self.db.get_op(op.id)
                    if current is None or current.status != "pending":
                        continue
                    if await self._execute(current, background=True) != "pending":
                        finished += 1
                        progressed = True
                if not progressed:
                    break
        return finished

    async def _execute(self, op: OpRow, *, background: bool) -> str:
        """Одна попытка. Итоговый статус: done | pending (повторим позже) | dead.

        dead не блокирует группу: следующие операции заметки пойдут (и, скорее всего, тоже
        получат понятную ошибку — например, «не нашёл заметку»).
        """
        now = self._now()
        if now - op.started_at > self.max_age:
            await self._dead(op, "не отправилась за OUTBOX_MAX_AGE", "expired", background)
            return "dead"
        try:
            result = await self._handlers[op.op](op)
        except VaultUnavailable as err:
            attempts = op.attempts + 1
            if attempts >= self.max_attempts:
                self.db.update_op(op.id, now, attempts=attempts)
                await self._dead(
                    op, f"{attempts} попыток: {err}", "attempts", background, attempts=attempts
                )
                return "dead"
            delay = float(BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)])
            if err.retry_after is not None:
                delay = max(delay, err.retry_after)
            at = now + timedelta(seconds=delay)
            self.db.update_op(
                op.id, now, attempts=attempts, next_attempt_at=at, last_error=str(err)
            )
            log.warning("Outbox #%d %s: %s — повтор в %s", op.id, op.op, err, at.isoformat())
            return "pending"
        except (VaultRejected, NoteNotFound, NoteAmbiguous, VaultError) as err:
            await self._dead(op, str(err), _kind(err), background, attempts=op.attempts + 1)
            return "dead"
        except Exception as err:  # ошибка бота — не теряем операцию, повторим по расписанию
            log.exception("Outbox #%d %s: непредвиденная ошибка", op.id, op.op)
            attempts = op.attempts + 1
            if attempts >= self.max_attempts:
                await self._dead(
                    op, f"{attempts} попыток: {err!r}"[:300], "attempts", background,
                    attempts=attempts,
                )  # fmt: skip
                return "dead"
            at = now + timedelta(seconds=BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)])
            self.db.update_op(
                op.id, now, attempts=attempts, next_attempt_at=at, last_error=repr(err)[:300]
            )
            return "pending"
        self.db.update_op(
            op.id, now, status="done", attempts=op.attempts + 1, result=result, last_error=None
        )
        log.info("Outbox #%d %s: отправлено", op.id, op.op)
        await self._after_done(op, result)
        return "done"

    async def _dead(
        self,
        op: OpRow,
        reason: str,
        kind: str,
        background: bool,
        *,
        attempts: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {"status": "dead", "last_error": reason, "result": {"error": kind}}
        if attempts is not None:
            fields["attempts"] = attempts
        self.db.update_op(op.id, self._now(), **fields)
        log.error("Outbox #%d %s: не отправлена — %s", op.id, op.op, reason)
        if not background:
            return  # пользователь ждёт ответа на кнопку/команду — ему покажет вызывающий код
        chat = op.chat_id or self.owner_id
        if self.bot is None or chat is None:
            return
        text = (
            f"❌ Не смог отправить на сайт: {escape(describe(op))}\n"
            f"Причина: {escape(reason)}\n/retry — попробовать ещё раз, /queue — очередь"
        )
        try:
            await self.bot.send_message(chat_id=chat, text=text)
        except Exception:
            log.exception("Outbox: не смог уведомить о dead #%d", op.id)

    async def _after_done(self, op: OpRow, result: dict[str, Any]) -> None:
        if op.op == "save":
            wanted, actual = op.payload.get("name", ""), result.get("name", "")
            if actual and actual != wanted:
                self._rename(op, wanted, actual)
        await self.notify_done(op.id)

    async def notify_done(self, op_id: int) -> None:
        """Отправленная операция с привязанным сообщением → сообщение получает итоговый текст."""
        op = self.db.get_op(op_id)
        if op is None or op.status != "done" or not op.notify or op.msg_id is None:
            return
        if self.bot is None:
            return
        text = str(op.notify.get("text", ""))
        result = op.result or {}
        if op.op == "save" and result.get("name") and op.payload.get("name"):
            text = text.replace(escape(op.payload["name"]), escape(result["name"]))
        markup = None
        if (note_id := op.notify.get("note_id")) is not None and (
            row := self.db.get_note(int(note_id))
        ) is not None:
            markup = kb.note_actions(row.id, detailed=row.detailed, read=row.read)
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=op.chat_id, message_id=op.msg_id, reply_markup=markup
            )
        except Exception:
            log.exception("Outbox: не смог обновить сообщение после #%d", op.id)

    def _rename(self, op: OpRow, wanted: str, actual: str) -> None:
        """Сайт добавил « (2)»: имя в базе заметок и в ждущих операциях группы → настоящее."""
        url = op.payload.get("url", "")
        if url:
            self.db.rename_notes(wanted, actual, url)
        now = self._now()
        for later in self.db.group_pending(op.grp, 2**62):
            if later.payload.get("name") == wanted:
                self.db.update_op(later.id, now, payload=later.payload | {"name": actual})

    # --- обработчики операций ---------------------------------------------------------------

    async def _save(self, op: OpRow) -> dict[str, Any]:
        p = op.payload
        name = await self.api.save(
            p["title"], p["markdown"], date.fromisoformat(p["day"]), idempotency_key=op.idem_key
        )
        return {"name": name}

    async def _set_status(self, op: OpRow) -> dict[str, Any]:
        p = op.payload
        name = await self.api.set_status(
            p["name"], p["url"], p["status"], skip=p.get("skip", []), idempotency_key=op.idem_key
        )
        return {"name": name}

    async def _replace(self, op: OpRow) -> dict[str, Any]:
        p = op.payload
        name = await self.api.replace(
            p["name"], p["url"], p["markdown"], skip=p.get("skip", []), idempotency_key=op.idem_key
        )
        return {"name": name}

    async def _delete(self, op: OpRow) -> dict[str, Any]:
        p = op.payload
        try:
            await self.api.delete(
                p["name"], p["url"], skip=p.get("skip", []), idempotency_key=op.idem_key
            )
        except NoteNotFound:
            return {"missing": True}  # уже нет — цель удаления достигнута
        return {}

    async def _task(self, op: OpRow) -> dict[str, Any]:
        p = op.payload
        return await self.api.create_task(
            p["text"],
            due=p.get("due"),
            priority=p.get("priority"),
            tags=p.get("tags") or None,
            idempotency_key=op.idem_key,
        )

    async def _capture(self, op: OpRow) -> dict[str, Any]:
        p = op.payload
        return await self.api.capture(p["text"], p["target"], idempotency_key=op.idem_key)

    # --- управление ------------------------------------------------------------------------

    def retry(self) -> int:
        """/retry: dead и отложенные — снова в работу сейчас; отсчёт попыток и возраста заново."""
        now = self._now()
        ops = [op for op in self.db.list_ops(("pending", "dead")) if not self._superseded(op)]
        for op in ops:
            self.db.update_op(
                op.id,
                now,
                status="pending",
                attempts=0,
                next_attempt_at=now,
                retried_at=now,
                result=None,
            )
        if ops:
            self._wake.set()
        return len(ops)

    def _superseded(self, op: OpRow) -> bool:
        """dead-операция, после которой в группе уже выполнено что-то ещё (порядок D5):
        повтор сломал бы порядок — например, воскресил бы уже удалённую заметку."""
        if op.status != "dead" or not op.grp.startswith("note:"):
            return False
        return any(
            later.id > op.id and later.grp == op.grp for later in self.db.list_ops(("done",))
        )

    def pending_names(self, grp: str) -> list[str]:
        """Имена заметок, чьё создание ещё ждёт в очереди (дубликат при выключенном сайте)."""
        return [
            str(op.payload["name"])
            for op in self.db.list_ops(("pending",))
            if op.op == "save" and op.grp == grp and op.payload.get("name")
        ]

    def pending_deletes(self, grp: str) -> set[str]:
        return {
            str(op.payload.get("name"))
            for op in self.db.list_ops(("pending",))
            if op.op == "delete" and op.grp == grp
        }

    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self, idle_s: float = IDLE_S) -> None:
        """Фоновый цикл: отправляет ждущие операции; просыпается по enqueue/retry или таймеру."""
        while True:
            try:
                await self.run_due()
            except Exception:
                log.exception("Outbox: сбой фонового цикла")
            delay = idle_s
            if (due := self.db.next_op_due()) is not None:
                delay = min(idle_s, max(1.0, (due - self._now()).total_seconds()))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)


class QueuedVault:
    """`VaultBackend` для режима api: каждая запись идёт через outbox (D4).

    Дошло сразу — как у GitVault. Сайт недоступен — `VaultQueued` (note_name, op_id): вызывающий
    код показывает «📮 …» и может привязать сообщение (`attach`), которое обновится после отправки.
    """

    def __init__(self, outbox: Outbox, api: ApiVault):
        self.outbox = outbox
        self.api = api

    async def ready(self) -> bool:
        return await self.api.ready()

    def attach(
        self, op_id: int, *, chat_id: int, msg_id: int, text: str, note_id: int | None = None
    ) -> None:
        self.outbox.attach(op_id, chat_id=chat_id, msg_id=msg_id, text=text, note_id=note_id)

    async def _submit(
        self, op: str, payload: dict[str, Any], grp: str, queued: str, name: str | None
    ) -> dict[str, Any]:
        op_id = self.outbox.enqueue(op, payload, grp=grp)
        row = await self.outbox.attempt(op_id)
        if row.status == "done":
            return row.result or {}
        if row.status == "dead":
            kind = (row.result or {}).get("error")
            message = row.last_error or "сайт отказал"
            if kind == "not_found":
                raise NoteNotFound(message, note_name=None)
            if kind == "ambiguous":
                raise NoteAmbiguous(message, note_name=None)
            raise VaultRejected(message)
        raise VaultQueued(queued, op_id=op_id, note_name=name)

    async def save(self, title: str, markdown: str, day: date) -> str:
        name = f"{day:%Y-%m-%d} {safe_filename(title)}"
        url = (frontmatter(markdown) or {}).get("url", "")
        payload = {
            "title": title,
            "markdown": markdown,
            "day": day.isoformat(),
            "name": name,
            "url": url,
        }
        result = await self._submit("save", payload, note_group(url, name), QUEUED_NOTE, name)
        return str(result.get("name") or name)

    async def set_status(
        self, name: str, url: str, status: str, *, skip: Collection[str] = ()
    ) -> str:
        payload = {"name": name, "url": url, "status": status, "skip": sorted(skip)}
        result = await self._submit(
            "set_status", payload, note_group(url, name), QUEUED_CHANGE, name
        )
        return str(result.get("name") or name)

    async def replace(
        self, name: str, url: str, markdown: str, *, skip: Collection[str] = ()
    ) -> str:
        payload = {"name": name, "url": url, "markdown": markdown, "skip": sorted(skip)}
        result = await self._submit("replace", payload, note_group(url, name), QUEUED_CHANGE, name)
        return str(result.get("name") or name)

    async def delete(self, name: str, url: str, *, skip: Collection[str] = ()) -> None:
        payload = {"name": name, "url": url, "skip": sorted(skip)}
        result = await self._submit("delete", payload, note_group(url, name), QUEUED_CHANGE, name)
        if result.get("missing"):
            raise NoteNotFound(f"Заметки «{name}» уже нет в хранилище.", note_name=None)

    async def unread(self) -> list[UnreadNote]:
        return await self.api.unread()

    async def find_by_url(self, url: str) -> list[str] | None:
        """lookup на сайте + ещё не отправленные заметки с этой ссылкой; сайт лежит → None."""
        try:
            names = await self.api.find_by_url(url) or []
        except VaultUnavailable as err:
            log.warning("Дубликаты: сайт недоступен (%s) — проверяю по локальной базе", err)
            return None
        grp = note_group(url)
        deleting = self.outbox.pending_deletes(grp)
        names = [n for n in names if n not in deleting]
        return names or self.outbox.pending_names(grp)

    async def create_task(
        self, text: str, *, due: str | None, priority: str | None, tags: list[str]
    ) -> dict[str, Any]:
        payload = {"text": text, "due": due, "priority": priority, "tags": tags}
        return await self._submit("task", payload, f"task:{uuid.uuid4()}", QUEUED_TASK, None)

    async def capture(self, text: str, target: str) -> dict[str, Any]:
        payload = {"text": text, "target": target}
        grp = f"capture:{uuid.uuid4()}"
        return await self._submit("capture", payload, grp, QUEUED_CAPTURE, None)

    async def ask(self, question: str) -> dict[str, Any]:
        return await self.api.ask(question)
