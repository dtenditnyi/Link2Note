import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from digest.errors import DigestError, VaultError
from digest.pipeline import make_note
from digest.schema import Digest
from digest.worker import Job, process_job, run_worker

VID = "dQw4w9WgXcQ"
CANON = f"https://www.youtube.com/watch?v={VID}"
DAY = date(2026, 11, 9)


def _digest(**kw) -> Digest:
    data = {
        "title": "Хоткеи <VS Code> & co",
        "emoji": "⌨️",
        "tldr": "Суть <b>жирно</b>",
        "key_points": ["a"],
        "tags": ["dev"],
        "read_minutes": 2,
        "confidence": "high",
    }
    data.update(kw)
    return Digest.model_validate(data)


def _http(status=200, payload=None):
    payload = payload or {"title": "t", "author_name": "Fireship"}
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload))
    )


def _llm(result=None, exc=None):
    llm = MagicMock()
    llm.digest_youtube = AsyncMock(return_value=result or _digest(), side_effect=exc)
    return llm


def _vault(name="2026-11-09 Хоткеи", exc=None):
    v = MagicMock()
    v.save = AsyncMock(return_value=name, side_effect=exc)
    return v


def _bot():
    b = MagicMock()
    b.edit_message_text = AsyncMock()
    return b


def _texts(bot) -> list[str]:
    return [c.kwargs["text"] for c in bot.edit_message_text.call_args_list]


JOB = Job(url=f"https://youtu.be/{VID}?si=x", hint="упор на хоткеи", chat_id=1, status_msg_id=10)


# --- pipeline ------------------------------------------------------------------------


async def test_pipeline_uses_canonical_url_and_author():
    llm = _llm()
    async with _http() as http:
        d, md = await make_note(JOB.url, JOB.hint, llm=llm, http=http, today=DAY)
    llm.digest_youtube.assert_awaited_once_with(CANON, "упор на хоткеи")
    assert 'author: "Fireship"' in md
    assert f'url: "{CANON}"' in md
    assert "date_added: 2026-11-09" in md
    assert d.title.startswith("Хоткеи")


async def test_pipeline_rejects_non_video():
    async with _http() as http:
        with pytest.raises(DigestError):
            await make_note(
                "https://www.youtube.com/@channel", "", llm=_llm(), http=http, today=DAY
            )


async def test_pipeline_private_video_skips_gemini():
    llm = _llm()
    async with _http(status=401) as http:
        with pytest.raises(DigestError):
            await make_note(JOB.url, "", llm=llm, http=http, today=DAY)
    llm.digest_youtube.assert_not_awaited()


# --- worker --------------------------------------------------------------------------


async def test_w1_success_message_escaped():
    bot, vault = _bot(), _vault()
    async with _http() as http:
        await process_job(JOB, bot=bot, llm=_llm(), http=http, vault=vault, today=lambda: DAY)
    texts = _texts(bot)
    assert texts[0] == "🧠 Смотрю видео…"
    assert texts[-1] == (
        "⌨️ <b>Хоткеи &lt;VS Code&gt; &amp; co</b>\n\n"
        "Суть &lt;b&gt;жирно&lt;/b&gt;\n\n"
        "📝 Заметка: «2026-11-09 Хоткеи»"
    )
    call = bot.edit_message_text.call_args
    assert call.kwargs["chat_id"] == 1 and call.kwargs["message_id"] == 10
    title, md, day = vault.save.call_args.args
    assert title == "Хоткеи <VS Code> & co" and day == DAY
    assert md.startswith("---\ntype: digest")


async def test_w2_digest_error_shown():
    bot = _bot()
    async with _http() as http:
        await process_job(
            JOB,
            bot=bot,
            llm=_llm(exc=DigestError("ИИ вернул <пусто>")),
            http=http,
            vault=_vault(),
            today=lambda: DAY,
        )
    assert _texts(bot)[-1] == "❌ Не получилось: ИИ вернул &lt;пусто&gt;"


async def test_w2_vault_error_shown():
    bot = _bot()
    err = VaultError("Заметка «x» сохранена на сервере, но не отправлена в GitHub", note_name="x")
    async with _http() as http:
        await process_job(
            JOB, bot=bot, llm=_llm(), http=http, vault=_vault(exc=err), today=lambda: DAY
        )
    assert "не отправлена в GitHub" in _texts(bot)[-1]
    assert _texts(bot)[-1].startswith("❌")


async def test_w2_unexpected_error_hidden(caplog):
    bot = _bot()
    async with _http() as http:
        await process_job(
            JOB,
            bot=bot,
            llm=_llm(exc=RuntimeError("secret path /app/x")),
            http=http,
            vault=_vault(),
            today=lambda: DAY,
        )
    last = _texts(bot)[-1]
    assert "внутренняя ошибка" in last
    assert "secret" not in last
    assert "secret path" in caplog.text


async def test_w3_worker_survives_edit_failure_and_keeps_order():
    bot = _bot()
    calls: list[int] = []

    async def edit(**kw):
        calls.append(kw["message_id"])
        if kw["message_id"] == 1:
            raise RuntimeError("message to edit not found")

    bot.edit_message_text = AsyncMock(side_effect=edit)
    queue: asyncio.Queue[Job] = asyncio.Queue()
    for i in (1, 2):
        queue.put_nowait(Job(url=JOB.url, hint="", chat_id=1, status_msg_id=i))

    async with _http() as http:
        task = asyncio.create_task(
            run_worker(queue, bot=bot, llm=_llm(), http=http, vault=_vault(), today=lambda: DAY)
        )
        await asyncio.wait_for(queue.join(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert 2 in calls
    assert calls.index(2) > max(i for i, m in enumerate(calls) if m == 1)  # W4: по порядку
