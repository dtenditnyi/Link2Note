"""Запись заметки в клон хранилища Obsidian: pull --rebase → новый файл → commit → push.

Бот пишет только новые файлы в свою папку, поэтому конфликты редки. Если всё же случился
конфликт rebase — он отменяется (`rebase --abort`), репозиторий никогда не остаётся
посреди rebase. Неотправленная заметка остаётся закоммиченной локально и уйдёт со следующей.
"""

import asyncio
import logging
import os
import re
import signal
from datetime import date
from pathlib import Path

from digest.errors import VaultError

log = logging.getLogger(__name__)

MAX_NAME = 80
PUSH_ATTEMPTS = 3
GIT_TIMEOUT_S = 120
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]]')  # Windows, Android и ссылки Obsidian


KILL_WAIT_S = 5
_POSIX = os.name == "posix"


class _GitFailed(Exception):
    pass


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Убить git и его детей (ssh, хуки). wait() ограничен: asyncio ждёт закрытия pipe,
    а их может держать «внук», переживший kill (на Windows группы процессов нет)."""
    try:
        if _POSIX:
            os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        else:
            proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_S)
    except TimeoutError:
        log.warning(
            "git (pid %s): pipe держит дочерний процесс, закрываем со своей стороны", proc.pid
        )
        # Публичного close() у Process нет; без этого transport и pipe утекают до выхода «внука».
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()


def safe_filename(title: str) -> str:
    name = re.sub(r"\s+", " ", _FORBIDDEN.sub("", title)).strip()
    name = name[:MAX_NAME].rstrip(" .")
    return name or "Без названия"


class Vault:
    def __init__(self, repo: Path, folder: str, author_name: str, author_email: str):
        self.repo = Path(repo)
        self.folder = folder
        self.dir = self.repo / folder
        self.author = (author_name, author_email)
        self.lock = asyncio.Lock()  # одна git-операция за раз

    def is_ready(self) -> bool:
        return (self.repo / ".git").exists()

    async def _git(self, *args: str) -> str:
        name, email = self.author
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            *args,
            cwd=self.repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # никогда не ждать ввода пароля
            start_new_session=_POSIX,  # своя группа процессов: убьём git вместе с ssh
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError) as exc:
            # Зависший git (полумёртвое соединение) не должен держать lock вечно,
            # а при остановке бота — оставлять живой процесс и .git/index.lock.
            await _kill(proc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _GitFailed(f"git {args[0]}: нет ответа {GIT_TIMEOUT_S} с") from exc
        if proc.returncode:
            raise _GitFailed(err.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
        return out.decode("utf-8", "replace")

    def _in_rebase(self) -> bool:
        git_dir = self.repo / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    async def _pull(self) -> bool:
        """Подтянуть чужие изменения. Сбой не фатален: заметку всё равно сохраним локально."""
        try:
            await self._git("pull", "--rebase", "--autostash", "--quiet")
            return True
        except _GitFailed as err:
            log.warning("git pull --rebase не удался: %s", err)
            if self._in_rebase():
                # Конфликт с неотправленным локальным коммитом сам не рассосётся:
                # все следующие push тоже будут падать, пока не разрешить вручную.
                log.error(
                    "Конфликт rebase в %s — push заблокирован. Разрешить вручную: "
                    "cd %s && git pull --rebase (поправить конфликт) && git push",
                    self.repo,
                    self.repo,
                )
                try:
                    await self._git("rebase", "--abort")
                except _GitFailed as abort_err:
                    log.error("git rebase --abort не удался: %s", abort_err)
            return False

    def _new_path(self, title: str, day: date) -> Path:
        base = f"{day:%Y-%m-%d} {safe_filename(title)}"
        path = self.dir / f"{base}.md"
        i = 2
        while path.exists():
            path = self.dir / f"{base} ({i}).md"
            i += 1
        return path

    async def save(self, title: str, markdown: str, day: date) -> str:
        """Сохраняет и отправляет заметку; возвращает её имя (без .md)."""
        async with self.lock:
            await self._pull()
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._new_path(title, day)
            note = path.stem
            path.write_text(markdown, encoding="utf-8", newline="\n")
            rel = path.relative_to(self.repo).as_posix()
            try:
                await self._git("add", "--", rel)
                message = f"📥 Link2Note: {' '.join(title.split())}"
                await self._git("commit", "-q", "-m", message, "--", rel)
            except _GitFailed as err:
                raise VaultError(
                    f"Не смог закоммитить заметку «{note}» в хранилище: {_short(err)}",
                    note_name=note,
                ) from err

            reason = ""
            for attempt in range(PUSH_ATTEMPTS):
                try:
                    await self._git("push", "--quiet")
                    return note
                except _GitFailed as err:
                    reason = _short(err)
                    log.warning("git push (попытка %d) не удался: %s", attempt + 1, err)
                    if attempt < PUSH_ATTEMPTS - 1:
                        await self._pull()
            raise VaultError(
                f"Заметка «{note}» сохранена на сервере, но не отправлена в GitHub "
                f"({reason}). Уйдёт вместе со следующей заметкой.",
                note_name=note,
            )


def _short(err: Exception) -> str:
    lines = [line for line in str(err).splitlines() if line.strip()]
    return (lines[-1] if lines else "ошибка git")[:200]
