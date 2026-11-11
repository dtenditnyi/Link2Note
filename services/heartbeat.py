"""Heartbeat для Docker HEALTHCHECK: бот раз в 30 с пишет время в файл из своего event loop.

Если loop завис (блокирующий вызов, дедлок), файл перестаёт обновляться и через 120 с
контейнер становится unhealthy. Проверка: `python -m services.heartbeat [путь] [макс_возраст]`.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

INTERVAL_S = 30
MAX_AGE_S = 120
DEFAULT_PATH = Path("data/heartbeat")


def write_heartbeat(path: Path, now: datetime) -> None:
    """Атомарно: временный файл → replace (проверка не прочитает половину строки)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(now.astimezone(UTC).isoformat(), encoding="utf-8")
    os.replace(tmp, path)


def heartbeat_age(path: Path, now: datetime) -> float | None:
    try:
        beat = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if beat.tzinfo is None:
        return None
    return (now - beat).total_seconds()


def is_healthy(path: Path, now: datetime, max_age_s: float = MAX_AGE_S) -> bool:
    age = heartbeat_age(path, now)
    return age is not None and -max_age_s < age < max_age_s


async def run_heartbeat(
    path: Path,
    interval_s: float = INTERVAL_S,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    while True:
        try:
            write_heartbeat(path, clock())
        except OSError:
            log.exception("Heartbeat: не смог записать %s", path)
        await asyncio.sleep(interval_s)


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT_PATH
    max_age = float(argv[1]) if len(argv) > 1 else MAX_AGE_S
    ok = is_healthy(path, datetime.now(UTC), max_age)
    print("healthy" if ok else f"unhealthy: heartbeat {path} старше {max_age:g} с или его нет")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
