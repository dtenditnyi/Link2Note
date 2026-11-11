"""Команды сайта Second Brain (только VAULT_BACKEND=api): /task, /note, /ask, /queue, /retry.

/task и /note идут через офлайн-очередь: сайт лежит — «📮 в очереди», сообщение обновится само.
/ask — напрямую (ответ нужен сейчас): сайт недоступен — честно «попробуй позже».
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape, unescape

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from digest.errors import VaultError, VaultQueued, VaultUnavailable
from digest.outbox import Outbox, QueuedVault, describe
from services.tasks import parse_task
from services.tg_format import split_markdown

log = logging.getLogger(__name__)
router = Router(name="brain")

NEED_API = "Это команда сайта Second Brain — включи VAULT_BACKEND=api."
TASK_USAGE = "Напиши задачу: <code>/task сдать лабу до пятницы ! #university</code>"
NOTE_USAGE = "Напиши текст: <code>/note идея …</code> или <code>/note неделя итог …</code>"
ASK_USAGE = "Задай вопрос: <code>/ask что я знаю про Docker?</code>"
ASK_DOWN = "🌐 Сайт Second Brain сейчас недоступен — попробуй позже."
WEEK_WORDS = ("неделя", "неделю", "недельная", "week")
MAX_TEXT = 4000


def _task_line(text: str, due, priority, tags) -> str:
    parts = [escape(text)]
    if due:
        parts.append(f"до {due:%d.%m.%Y}")
    if priority == "high":
        parts.append("❗ важно")
    if tags:
        parts.append(" ".join(f"#{t}" for t in tags))
    return " · ".join(parts)


async def add_task(message: Message, brain: QueuedVault, raw: str, now: datetime) -> None:
    """/task и голосовое «добавь задачу …»."""
    draft = parse_task(raw, now)
    if not draft.text:
        await message.answer(TASK_USAGE)
        return
    line = _task_line(draft.text, draft.due, draft.priority, draft.tags)
    extra = ""
    if draft.unknown_tags:
        extra = "\n⚠️ Таких тегов нет в хранилище, пропустил: " + ", ".join(
            f"#{escape(t)}" for t in draft.unknown_tags
        )
    done = f"✅ Задача добавлена: {line}{extra}"
    try:
        await brain.create_task(
            draft.text,
            due=draft.due.isoformat() if draft.due else None,
            priority=draft.priority,
            tags=draft.tags,
        )
    except VaultQueued as err:
        reply = await message.answer(f"{escape(str(err))}\n{line}{extra}")
        brain.attach(err.op_id, chat_id=message.chat.id, msg_id=reply.message_id, text=done)
        return
    except VaultError as err:
        await message.answer(f"❌ Задачу не добавил: {escape(str(err))}")
        return
    await message.answer(done)


async def add_note(message: Message, brain: QueuedVault, raw: str) -> None:
    """/note и голосовое «запиши в заметки …». Первое слово «неделя» — в недельную заметку."""
    text = raw.strip()
    target = "inbox"
    first, _, rest = text.partition(" ")
    if first.lower().rstrip(":,") in WEEK_WORDS:
        target, text = "weekly", rest.strip()
    if not text:
        await message.answer(NOTE_USAGE)
        return
    text = text[:MAX_TEXT]
    where = "в недельную заметку" if target == "weekly" else "во «Входящие»"
    done = f"📥 Записал {where}: {escape(text[:200])}"
    try:
        await brain.capture(text, target)
    except VaultQueued as err:
        reply = await message.answer(escape(str(err)))
        brain.attach(err.op_id, chat_id=message.chat.id, msg_id=reply.message_id, text=done)
        return
    except VaultError as err:
        await message.answer(f"❌ Не записал: {escape(str(err))}")
        return
    await message.answer(done)


@router.message(Command("task"))
async def cmd_task(
    message: Message,
    command: CommandObject,
    brain: QueuedVault | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if brain is None:
        await message.answer(NEED_API)
        return
    if not (command.args or "").strip():
        await message.answer(TASK_USAGE)
        return
    await add_task(message, brain, command.args or "", clock())


@router.message(Command("note"))
async def cmd_note(
    message: Message, command: CommandObject, brain: QueuedVault | None = None
) -> None:
    if brain is None:
        await message.answer(NEED_API)
        return
    await add_note(message, brain, command.args or "")


@router.message(Command("ask"))
async def cmd_ask(
    message: Message, command: CommandObject, brain: QueuedVault | None = None
) -> None:
    if brain is None:
        await message.answer(NEED_API)
        return
    question = (command.args or "").strip()
    if not question:
        await message.answer(ASK_USAGE)
        return
    try:
        answer = await brain.ask(question[:MAX_TEXT])
    except VaultUnavailable:
        await message.answer(ASK_DOWN)
        return
    except VaultError as err:
        await message.answer(f"❌ Сайт не ответил на вопрос: {escape(str(err))}")
        return
    chunks = split_markdown(str(answer.get("answer_markdown") or "")) or ["(пустой ответ)"]
    sources = [s.get("name") or s.get("path") for s in answer.get("sources") or []]
    if sources:
        line = "📎 Источники: " + ", ".join(f"«{escape(str(s))}»" for s in sources[:10])
        if len(chunks[-1]) + len(line) + 2 <= 4096:
            chunks[-1] += "\n\n" + line
        else:
            chunks.append(line)
    for chunk in chunks:
        try:
            await message.answer(chunk, disable_web_page_preview=True)
        except TelegramBadRequest:  # Telegram не разобрал HTML — тот же текст без разметки
            log.warning("/ask: Telegram не принял HTML, шлю простым текстом")
            plain = unescape(re.sub(r"<[^>]+>", "", chunk))
            await message.answer(plain, parse_mode=None, disable_web_page_preview=True)


def _when(at: datetime, tz) -> str:
    return at.astimezone(tz).strftime("%d.%m %H:%M") if tz else at.strftime("%d.%m %H:%M UTC")


@router.message(Command("queue"))
async def cmd_queue(message: Message, outbox: Outbox | None = None, tz=None) -> None:
    if outbox is None:
        await message.answer("Очередь сайта есть только в режиме VAULT_BACKEND=api.")
        return
    ops = outbox.db.list_ops(("pending", "dead"))
    if not ops:
        await message.answer("📮 Очередь сайта пуста — всё отправлено.")
        return
    pending = [o for o in ops if o.status == "pending"]
    dead = [o for o in ops if o.status == "dead"]
    lines = [f"📮 <b>Очередь сайта</b>: ждут {len(pending)}, не отправлены {len(dead)}"]
    for op in ops[:15]:
        why = escape((op.last_error or "")[:120])
        if op.status == "pending":
            state = f"⏳ попытка {op.attempts}, следующая {_when(op.next_attempt_at, tz)}"
        else:
            state = "❌ не отправлена"
        lines.append(f"• {escape(describe(op))} — {state}" + (f"\n  <i>{why}</i>" if why else ""))
    if len(ops) > 15:
        lines.append(f"…и ещё {len(ops) - 15}")
    if dead or pending:
        lines.append("\n/retry — отправить сейчас")
    await message.answer("\n".join(lines))


@router.message(Command("retry"))
async def cmd_retry(message: Message, outbox: Outbox | None = None) -> None:
    if outbox is None:
        await message.answer("Очередь сайта есть только в режиме VAULT_BACKEND=api.")
        return
    revived = outbox.retry()
    if not revived:
        await message.answer("📮 Очередь сайта пуста — повторять нечего.")
        return
    await outbox.run_due()
    counts = outbox.db.count_ops()
    await message.answer(
        f"🔁 Повторил: {revived}. Ждут отправки: {counts['pending']}, "
        f"не отправлены: {counts['dead']}."
    )
