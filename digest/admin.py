"""Служебные команды для deploy-скриптов (внутри контейнера или локально):

    python -m digest.admin [--db data/link2note.db] queue        — размер очереди сайта
    python -m digest.admin [--db …] backup <файл[.gz]>          — онлайн-бэкап SQLite
    python -m digest.admin api-health                           — отвечает ли сайт (режим api)

Бэкап — через sqlite3 backup API (согласованная копия, пока бот пишет), не копированием файла.
Токены и содержимое заметок не печатаются.
"""

import argparse
import asyncio
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from database import Database


def _db_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("DB_PATH") or "data/link2note.db")


def cmd_queue(db_path: Path) -> int:
    if not db_path.exists():
        print(f"нет базы {db_path}", file=sys.stderr)
        return 1
    db = Database(db_path)
    db.init()  # идемпотентно: старая база получит vault_ops
    counts = db.count_ops()
    print(f"pending: {counts['pending']}")
    print(f"dead: {counts['dead']}")
    print(f"done: {counts['done']}")
    pending = db.list_ops(("pending",))
    if pending:
        oldest = min(op.created_at for op in pending)
        print(f"oldest_pending: {oldest.isoformat()}")
        print(f"next_attempt: {min(op.next_attempt_at for op in pending).isoformat()}")
    return 0


def backup(db_path: Path, dst: Path) -> None:
    """Согласованная копия работающей базы; .gz — сжать."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dst.parent) as tmp:
        raw = Path(tmp) / "backup.db"
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        out = sqlite3.connect(raw)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        check = sqlite3.connect(raw)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if ok != "ok":
            raise RuntimeError(f"копия базы повреждена: {ok}")
        if dst.suffix == ".gz":
            with raw.open("rb") as f_in, gzip.open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copyfile(raw, dst)


def cmd_backup(db_path: Path, dst: Path) -> int:
    if not db_path.exists():
        print(f"нет базы {db_path}", file=sys.stderr)
        return 1
    backup(db_path, dst)
    print(f"backup: {dst} ({dst.stat().st_size} байт)")
    return 0


async def _api_health() -> int:
    from config import get_settings
    from digest.vault_api import ApiVault

    s = get_settings()
    if s.vault_backend != "api":
        print("VAULT_BACKEND=git — сайт не используется")
        return 0
    api = ApiVault(s.second_brain_api_url, s.second_brain_api_token, s.vault_folder, timeout_s=5)
    try:
        info = await api.health()
    except Exception as err:  # noqa: BLE001 — любая ошибка = недоступен
        print(f"api: недоступен ({type(err).__name__}: {err})")
        return 1
    finally:
        await api.aclose()
    print(f"api: {info.get('status')} (synced {info.get('vault_synced_at')})")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m digest.admin")
    p.add_argument("--db", help="путь к базе (по умолчанию DB_PATH или data/link2note.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("queue", help="размер очереди сайта (vault_ops)")
    b = sub.add_parser("backup", help="онлайн-бэкап SQLite")
    b.add_argument("dst")
    sub.add_parser("api-health", help="проверить сайт Second Brain")
    args = p.parse_args(argv)
    db_path = _db_path(args.db)
    if args.cmd == "queue":
        return cmd_queue(db_path)
    if args.cmd == "backup":
        return cmd_backup(db_path, Path(args.dst))
    return asyncio.run(_api_health())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
