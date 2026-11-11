"""Часть D: разбор /task, Markdown → HTML для /ask, голосовые команды, хендлеры сайта."""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject

from digest.errors import VaultQueued, VaultRejected, VaultUnavailable
from handlers.brain import NEED_API, cmd_ask, cmd_note, cmd_queue, cmd_retry, cmd_task
from services.brain_intent import voice_command
from services.tasks import parse_task
from services.tg_format import md_to_html, split_markdown
from tests.conftest import TZ, make_message, sent_texts

WED = datetime(2026, 11, 11, 12, 0, tzinfo=TZ)  # среда


@pytest.mark.parametrize(
    ("raw", "text", "due", "priority", "tags"),
    [
        ("купить хлеб", "купить хлеб", None, None, []),
        ("купить хлеб завтра", "купить хлеб", date(2026, 11, 12), None, []),
        ("до пятницы сдать лабу", "сдать лабу", date(2026, 11, 13), None, []),
        ("сдать лабу в среду", "сдать лабу", date(2026, 11, 18), None, []),  # сегодня среда
        ("отчёт до 15.11 !", "отчёт", date(2026, 11, 15), "high", []),
        ("отчёт к 01.02", "отчёт", date(2027, 2, 1), None, []),
        ("отчёт 31.02", "отчёт 31.02", None, None, []),
        ("позвонить через 3 дня", "позвонить", date(2026, 11, 14), None, []),
        ("купить ! молоко #cooking #Health #хрень", "купить молоко", None, "high",
         ["cooking", "health"]),
        ("послезавтра !! важное", "важное", date(2026, 11, 13), "high", []),
        ("восклицание! в тексте", "восклицание! в тексте", None, None, []),
    ],
)  # fmt: skip
def test_parse_task(raw, text, due, priority, tags):
    d = parse_task(raw, WED)
    assert (d.text, d.due, d.priority, d.tags) == (text, due, priority, tags)


def test_parse_task_unknown_tags_reported():
    assert parse_task("x #foo #dev #foo", WED).unknown_tags == ["foo"]


def test_md_to_html_escapes_and_formats():
    html = md_to_html(
        "# Заголовок\n**жирно** и *курсив* `a<b>` <script>\n- пункт\n[сайт](https://x.y)"
    )
    assert "<b>Заголовок</b>" in html and "<b>жирно</b>" in html and "<i>курсив</i>" in html
    assert "<code>a&lt;b&gt;</code>" in html and "&lt;script&gt;" in html
    assert "• пункт" in html and '<a href="https://x.y">сайт</a>' in html


def test_md_code_block_not_formatted():
    html = md_to_html("```python\nx = a**2 * b**2  # <tag>\n```")
    assert html == "<pre>x = a**2 * b**2  # &lt;tag&gt;</pre>"


def test_split_markdown_limits_and_balanced_tags():
    md = "\n\n".join(f"**Абзац {i}** " + "слово <x> " * 60 for i in range(60))
    md += "\n\n```\n" + "код\n" * 2000 + "```"
    chunks = split_markdown(md)
    assert len(chunks) > 3
    for c in chunks:
        assert len(c) <= 4096
        for tag in ("b", "i", "pre", "code"):
            assert c.count(f"<{tag}>") == c.count(f"</{tag}>")


def test_split_huge_single_line():
    chunks = split_markdown("а" * 20000)
    assert sum(c.count("а") for c in chunks) == 20000  # ничего не потеряно
    assert all(len(c) <= 4096 for c in chunks)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Добавь задачу купить молоко", ("task", "купить молоко")),
        ("запиши задачу: сдать лабу до пятницы.", ("task", "сдать лабу до пятницы")),
        ("Запиши в заметки идея для поста", ("note", "идея для поста")),
        ("запиши во входящие купить книгу", ("note", "купить книгу")),
        ("добавь задачу", None),
        ("напомни добавить задачу завтра", None),
        ("купи молоко", None),
    ],
)
def test_voice_command(text, expected):
    assert voice_command(text) == expected


# --- хендлеры (хранилище — мок QueuedVault) -----------------------------------------


def _cmd(name, args):
    return CommandObject(prefix="/", command=name, args=args)


def _brain(**kw):
    b = MagicMock()
    b.create_task = AsyncMock(**kw)
    b.capture = AsyncMock(**kw)
    b.ask = AsyncMock(**kw)
    return b


async def test_task_success_text():
    brain = _brain(return_value={"id": "t1"})
    msg = make_message("/task")
    await cmd_task(msg, _cmd("task", "лаба до пятницы ! #university #bad"), brain=brain,
                   clock=lambda: WED)  # fmt: skip
    brain.create_task.assert_awaited_once_with(
        "лаба", due="2026-11-13", priority="high", tags=["university"]
    )
    text = sent_texts(msg.answer)[-1]
    assert "✅" in text and "13.11.2026" in text and "#university" in text and "#bad" in text


async def test_task_queued_attaches_reply():
    brain = _brain(side_effect=VaultQueued("📮 Сайт недоступен", op_id=7))
    msg = make_message("/task")
    await cmd_task(msg, _cmd("task", "хлеб"), brain=brain, clock=lambda: WED)
    assert sent_texts(msg.answer)[-1].startswith("📮")
    kw = brain.attach.call_args.kwargs
    assert brain.attach.call_args.args == (7,) and kw["msg_id"] == 700
    assert kw["text"].startswith("✅ Задача добавлена")


async def test_task_rejected_and_usage_and_git_mode():
    msg = make_message("/task")
    await cmd_task(msg, _cmd("task", "x"), brain=_brain(side_effect=VaultRejected("403")),
                   clock=lambda: WED)  # fmt: skip
    assert sent_texts(msg.answer)[-1].startswith("❌")
    msg = make_message("/task")
    await cmd_task(msg, _cmd("task", None), brain=_brain(), clock=lambda: WED)
    assert "/task" in sent_texts(msg.answer)[-1]
    msg = make_message("/task")
    await cmd_task(msg, _cmd("task", "x"), brain=None, clock=lambda: WED)
    assert sent_texts(msg.answer) == [NEED_API]


async def test_note_targets_and_html_escape():
    brain = _brain(return_value={})
    msg = make_message("/note")
    await cmd_note(msg, _cmd("note", "Неделя: <итог>"), brain=brain)
    brain.capture.assert_awaited_once_with("<итог>", "weekly")
    assert "&lt;итог&gt;" in sent_texts(msg.answer)[-1]
    await cmd_note(make_message("/note"), _cmd("note", "недельный обзор"), brain=brain)
    assert brain.capture.await_args.args == ("недельный обзор", "inbox")


async def test_ask_down_and_rejected():
    msg = make_message("/ask")
    await cmd_ask(msg, _cmd("ask", "?"), brain=_brain(side_effect=VaultUnavailable("x")))
    assert "недоступен" in sent_texts(msg.answer)[-1]
    msg = make_message("/ask")
    await cmd_ask(msg, _cmd("ask", "?"), brain=_brain(side_effect=VaultRejected("403")))
    assert sent_texts(msg.answer)[-1].startswith("❌")


async def test_ask_sources_escaped():
    brain = _brain(return_value={"answer_markdown": "ok", "sources": [{"name": "<b>x"}]})
    msg = make_message("/ask")
    await cmd_ask(msg, _cmd("ask", "?"), brain=brain)
    assert sent_texts(msg.answer) == ["ok\n\n📎 Источники: «&lt;b&gt;x»"]


async def test_queue_and_retry_in_git_mode():
    msg = make_message("/queue")
    await cmd_queue(msg, outbox=None)
    assert "VAULT_BACKEND=api" in sent_texts(msg.answer)[-1]
    msg = make_message("/retry")
    await cmd_retry(msg, outbox=None)
    assert "VAULT_BACKEND=api" in sent_texts(msg.answer)[-1]


async def test_voice_routes_task_without_gemini(tmp_path):
    from handlers.voice import on_voice

    msg = make_message(None)
    msg.voice.duration = 3
    msg.voice.file_unique_id = "v1"
    msg.answer = AsyncMock(return_value=MagicMock(edit_text=AsyncMock(), message_id=700))
    transcriber = MagicMock()
    transcriber.transcribe = AsyncMock(return_value="Добавь задачу купить молоко завтра")
    intent = MagicMock()
    intent.parse = AsyncMock()
    brain = _brain(return_value={"id": "t"})
    bot = MagicMock()
    bot.download = AsyncMock()
    await on_voice(msg, bot=bot, drafts=MagicMock(), transcriber=transcriber, intent=intent,
                   memory=MagicMock(), clock=lambda: WED, tz=TZ, voice_max_seconds=60,
                   brain=brain)  # fmt: skip
    intent.parse.assert_not_awaited()
    brain.create_task.assert_awaited_once_with(
        "купить молоко", due="2026-11-12", priority=None, tags=[]
    )


# --- ревью C/D/E: HTML для Telegram ---------------------------------------------------


def _valid(html: str) -> bool:
    from services.tg_format import _balanced

    return _balanced(html) and len(html) <= 4096


def test_split_code_block_of_special_chars_keeps_everything():
    """Блок из одних <>& раньше резался посередине тега/сущности и терял текст."""
    raw = "<>&" * 6000
    chunks = split_markdown(f"```\n{raw}\n```")
    assert all(_valid(c) for c in chunks)
    from html import unescape

    text = "".join(unescape(c.removeprefix("<pre>").removesuffix("</pre>")) for c in chunks)
    assert text == raw


def test_link_with_quote_escaped():
    html = md_to_html('[сайт](https://x.com/a"b)')
    assert 'href="https://x.com/a&quot;b"' in html


def test_no_italic_inside_bare_url():
    assert md_to_html("см. https://ex.com/_x_/y и _курсив_") == (
        "см. https://ex.com/_x_/y и <i>курсив</i>"
    )


def test_crossed_markup_falls_back_to_plain_text():
    html = md_to_html("**a *b** c*")
    assert _valid(html) and "<b>" not in html


async def test_ask_falls_back_to_plain_text_on_telegram_error():
    from aiogram.exceptions import TelegramBadRequest

    brain = _brain(return_value={"answer_markdown": "**ok**", "sources": []})
    msg = make_message("/ask")
    msg.answer = AsyncMock(side_effect=[TelegramBadRequest(MagicMock(), "can't parse"), None])
    await cmd_ask(msg, _cmd("ask", "?"), brain=brain)
    last = msg.answer.await_args_list[-1]
    assert last.args[0] == "ok" and last.kwargs.get("parse_mode") is None


@pytest.mark.parametrize(
    ("raw", "text", "due"),
    [
        ("обновить python 3.12", "обновить python 3.12", None),
        ("прочитать 2.5 главы", "прочитать 2.5 главы", None),
        ("настроить среду разработки", "настроить среду разработки", None),
        ("сдать отчёт 20.11.2026", "сдать отчёт", date(2026, 11, 20)),
        ("сдать отчёт по пятницу", "сдать отчёт", date(2026, 11, 13)),
    ],
)
def test_parse_task_no_false_dates(raw, text, due):
    """Ревью C/D/E #4: числа и дни недели без предлога — не срок."""
    d = parse_task(raw, WED)
    assert (d.text, d.due) == (text, due)
