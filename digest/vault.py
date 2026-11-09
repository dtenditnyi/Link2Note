"""Запись заметки в клон хранилища Obsidian: pull --rebase → новый файл → commit → push.

Бот пишет новые файлы в свою папку, а по кнопкам (этап 3) меняет или удаляет свои заметки:
статус во frontmatter, подробный конспект в тот же файл. Заметку, которую я переименовал или
переместил в Obsidian, ищем по `url:` во frontmatter. Конфликты редки. Если всё же случился
конфликт rebase — он отменяется (`rebase --abort`), репозиторий никогда не остаётся
посреди rebase. Неотправленная заметка остаётся закоммиченной локально и уйдёт со следующей.
"""

import asyncio
import json
import logging
import os
import re
import signal
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from digest.classifier import normalize
from digest.errors import NoteAmbiguous, NoteNotFound, VaultError

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


@dataclass(frozen=True)
class UnreadNote:
    name: str  # имя файла без .md
    title: str  # первый заголовок «# …» (с эмодзи)
    date_added: date | None
    read_minutes: int


_FRONTMATTER = re.compile(r"\A---\n(.*?\n)---\n", re.S)


def _unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value.strip('"')
    return value


def frontmatter(text: str) -> dict[str, str] | None:
    """Плоский YAML-frontmatter заметки: только строки «ключ: значение» (наш шаблон)."""
    m = _FRONTMATTER.match(text)
    if m is None:
        return None
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key and not key.startswith((" ", "-")):
            out[key.strip()] = _unquote(value)
    return out


def with_status(text: str, status: str) -> str:
    """Меняет status только во frontmatter — такая же строка в теле заметки не трогается."""
    m = _FRONTMATTER.match(text)
    if m is None:
        return text
    block = m.group(1)
    if re.search(r"(?m)^status:", block):
        block = re.sub(r"(?m)^status:.*$", f"status: {status}", block, count=1)
    else:
        block += f"status: {status}\n"
    return f"---\n{block}---\n" + text[m.end() :]


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


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

    async def _commit_push(
        self, path: Path, message: str, done: str, *, restore: bool = False
    ) -> None:
        """add (в т.ч. удаление) → commit → push с повторами. done — «сохранена», «удалена»…

        restore=True (правка существующей заметки): коммит не удался — файл возвращаем из HEAD,
        чтобы незакоммиченное изменение не висело в рабочей копии (note_name=None — ничего нет).
        """
        note = path.stem
        rel = path.relative_to(self.repo).as_posix()
        try:
            await self._git("add", "-A", "--", rel)
            await self._git("commit", "-q", "-m", message, "--", rel)
        except _GitFailed as err:
            if restore:
                try:
                    await self._git("reset", "-q", "--", rel)
                    await self._git("checkout", "-q", "--", rel)
                except _GitFailed as restore_err:
                    log.error("Не смог вернуть %s после сбоя коммита: %s", rel, restore_err)
            raise VaultError(
                f"Не смог закоммитить заметку «{note}» в хранилище: {_short(err)}",
                note_name=None if restore else note,
            ) from err

        reason = ""
        for attempt in range(PUSH_ATTEMPTS):
            try:
                await self._git("push", "--quiet")
                return
            except _GitFailed as err:
                reason = _short(err)
                log.warning("git push (попытка %d) не удался: %s", attempt + 1, err)
                if attempt < PUSH_ATTEMPTS - 1:
                    await self._pull()
        raise VaultError(
            f"Заметка «{note}» {done} на сервере, но не отправлена в GitHub "
            f"({reason}). Уйдёт вместе со следующей заметкой.",
            note_name=note,
        )

    async def save(self, title: str, markdown: str, day: date) -> str:
        """Сохраняет и отправляет заметку; возвращает её имя (без .md)."""
        async with self.lock:
            await self._pull()
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._new_path(title, day)
            path.write_text(markdown, encoding="utf-8", newline="\n")
            message = f"📥 Link2Note: {' '.join(title.split())}"
            await self._commit_push(path, message, "сохранена")
            return path.stem

    # --- Этап 3: изменение своих заметок ------------------------------------------------

    def _locate(self, name: str, url: str, skip: Collection[str] = ()) -> Path:
        """Своя папка по имени; иначе — единственная заметка хранилища с этим url.

        skip — имена других заметок с тем же url («заново»): удалить или отметить их вместо
        этой нельзя. Совпадений больше одного — не угадываем.
        """
        path = self.dir / f"{name}.md"
        if path.is_file():
            return path
        # Ссылки сравниваем по ключу: в заметке и в базе одна ссылка бывает в разных формах
        # (Reels с ?dlrf=…, youtu.be и watch?v=).
        key = normalize(url)
        found = []
        for candidate in sorted(self.repo.rglob("*.md")):
            if ".git" in candidate.relative_to(self.repo).parts or candidate.stem in skip:
                continue
            fm = frontmatter(_read(candidate))
            if fm and fm.get("type") == "digest" and fm.get("url") and normalize(fm["url"]) == key:
                found.append(candidate)
        if len(found) == 1:
            return found[0]
        if found:
            names = ", ".join(f"«{p.stem}»" for p in found)
            raise NoteAmbiguous(
                f"Нашёл несколько заметок с этой ссылкой: {names} — не знаю, какую.",
                note_name=None,
            )
        raise NoteNotFound(
            f"Не нашёл заметку «{name}» в хранилище — удалена или ссылка в ней изменена?",
            note_name=None,
        )

    async def set_status(
        self, name: str, url: str, status: str, *, skip: Collection[str] = ()
    ) -> str:
        """status во frontmatter → commit + push. Возвращает текущее имя заметки."""
        async with self.lock:
            await self._pull()
            path = self._locate(name, url, skip)
            text = _read(path)
            new = with_status(text, status)
            if new != text:
                path.write_text(new, encoding="utf-8", newline="\n")
                label = "прочитано" if status == "read" else f"status {status}"
                await self._commit_push(
                    path, f"✅ Link2Note: {label} {path.stem}", "изменена", restore=True
                )
            return path.stem

    async def replace(
        self, name: str, url: str, markdown: str, *, skip: Collection[str] = ()
    ) -> str:
        """Новый текст в тот же файл («Подробнее»). Возвращает текущее имя заметки."""
        async with self.lock:
            await self._pull()
            path = self._locate(name, url, skip)
            if _read(path) != markdown:
                path.write_text(markdown, encoding="utf-8", newline="\n")
                await self._commit_push(
                    path, f"🔁 Link2Note: подробнее {path.stem}", "обновлена", restore=True
                )
            return path.stem

    async def delete(self, name: str, url: str, *, skip: Collection[str] = ()) -> None:
        async with self.lock:
            await self._pull()
            path = self._locate(name, url, skip)
            path.unlink()
            await self._commit_push(
                path, f"🗑 Link2Note: удалено {path.stem}", "удалена", restore=True
            )

    async def unread(self) -> list[UnreadNote]:
        """Непрочитанные конспекты своей папки (после pull), самые старые первыми."""
        async with self.lock:
            await self._pull()
            if not self.dir.is_dir():
                return []
            notes = []
            for path in self.dir.rglob("*.md"):
                text = _read(path)
                fm = frontmatter(text)
                if not fm or fm.get("type") != "digest" or fm.get("status") != "unread":
                    continue
                try:
                    added: date | None = date.fromisoformat(fm.get("date_added", ""))
                except ValueError:
                    added = None
                try:
                    minutes = int(fm.get("read_minutes", "0"))
                except ValueError:
                    minutes = 0
                notes.append(UnreadNote(path.stem, _title(text, path.stem), added, minutes))
        notes.sort(key=lambda n: (n.date_added or date.max, n.name))
        return notes


def _short(err: Exception) -> str:
    lines = [line for line in str(err).splitlines() if line.strip()]
    return (lines[-1] if lines else "ошибка git")[:200]
