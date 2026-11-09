from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx

from digest.cli import run
from digest.schema import Digest

DAY = date(2026, 11, 9)
URL = "https://youtu.be/dQw4w9WgXcQ"


def _deps():
    llm = MagicMock()
    llm.digest_youtube = AsyncMock(
        return_value=Digest.model_validate(
            {
                "title": "CLI тест",
                "emoji": "🧪",
                "tldr": "Суть.",
                "key_points": ["a"],
                "tags": ["dev"],
                "read_minutes": 1,
                "confidence": "high",
            }
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"author_name": "A"}))
    )
    vault = MagicMock()
    vault.save = AsyncMock(return_value="2026-11-09 CLI тест")
    return llm, http, vault


async def test_k1_prints_markdown_without_saving(capsys):
    llm, http, vault = _deps()
    async with http:
        code = await run([URL, "--hint", "кратко"], llm=llm, http=http, vault=vault, today=DAY)
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("---\ntype: digest")
    assert "# 🧪 CLI тест" in out
    vault.save.assert_not_awaited()
    assert llm.digest_youtube.call_args.args[1] == "кратко"


async def test_k1_save_flag_writes_to_vault(capsys):
    llm, http, vault = _deps()
    async with http:
        code = await run([URL, "--save"], llm=llm, http=http, vault=vault, today=DAY)
    assert code == 0
    vault.save.assert_awaited_once()
    assert "2026-11-09 CLI тест" in capsys.readouterr().err


async def test_k1_save_without_vault_fails(capsys):
    llm, http, _ = _deps()
    async with http:
        code = await run([URL, "--save"], llm=llm, http=http, vault=None, today=DAY)
    assert code == 2
    assert "VAULT_REPO" in capsys.readouterr().err
    llm.digest_youtube.assert_not_awaited()


async def test_k1_digest_error_exit_code(capsys):
    llm, http, vault = _deps()
    async with http:
        code = await run(
            ["https://habr.com/ru/articles/1/"], llm=llm, http=http, vault=vault, today=DAY
        )
    assert code == 1
    assert "❌" in capsys.readouterr().err
