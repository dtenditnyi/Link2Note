"""Конспект без Telegram: uv run python -m digest.cli <url> [--hint "…"] [--save]

Markdown печатается в stdout; с --save заметка ещё и коммитится/пушится в VAULT_REPO.
Нужен .env с GEMINI_API_KEY (и BOT_TOKEN/OWNER_ID — их требует общий config).
"""

import argparse
import asyncio
import io
import sys
from datetime import date, datetime

import httpx

from digest.errors import DigestError
from digest.pipeline import DigestModel, make_note
from digest.worker import NoteStore


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m digest.cli", description=__doc__)
    p.add_argument("url", help="ссылка на YouTube-видео")
    p.add_argument("--hint", default="", help="пожелание к конспекту")
    p.add_argument("--save", action="store_true", help="сохранить в VAULT_REPO и сделать push")
    return p


async def run(
    argv: list[str],
    *,
    llm: DigestModel,
    http: httpx.AsyncClient,
    vault: NoteStore | None,
    today: date,
) -> int:
    args = _parser().parse_args(argv)
    if args.save and vault is None:
        print("❌ --save: VAULT_REPO не настроен или это не git-репозиторий", file=sys.stderr)
        return 2
    try:
        d, md = await make_note(args.url, args.hint, llm=llm, http=http, today=today)
        print(md, end="")
        if args.save:
            assert vault is not None
            name = await vault.save(d.title, md, today)
            print(f"📝 Сохранено: «{name}»", file=sys.stderr)
    except DigestError as err:
        print(f"❌ {err}", file=sys.stderr)
        return 1
    return 0


async def _main(argv: list[str]) -> int:
    from config import get_settings
    from digest.llm import DigestLLM
    from digest.vault import Vault

    settings = get_settings()
    vault = None
    if settings.vault_repo:
        candidate = Vault(
            settings.vault_repo,
            settings.vault_folder,
            settings.git_author_name,
            settings.git_author_email,
        )
        vault = candidate if candidate.is_ready() else None
    llm = DigestLLM(settings.gemini_api_key, settings.gemini_model, settings.gemini_timeout_s)
    async with httpx.AsyncClient(timeout=15) as http:
        return await run(
            argv,
            llm=llm,
            http=http,
            vault=vault,
            today=datetime.now(settings.timezone).date(),
        )


if __name__ == "__main__":
    # Windows-консоль по умолчанию не в UTF-8 — эмодзи и кириллица иначе падают.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(_main(sys.argv[1:])))
