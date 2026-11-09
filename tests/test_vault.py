"""Vault на настоящем git: bare-репозиторий во tmp_path играет роль GitHub. Сети нет."""

import asyncio
import subprocess
from datetime import date
from pathlib import Path

import pytest

from digest.errors import VaultError
from digest.vault import Vault, safe_filename

FOLDER = "03 - Resources/📥 Из ссылок"
DAY = date(2026, 11, 9)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        # core.quotepath=false — иначе git выводит кириллицу/эмодзи в путях как \320\230…
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def clone_repo(remote: Path, dest: Path) -> None:
    """Синхронный хелпер: в тестах блокирующий git допустим (ASYNC221 — про боевой код)."""
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], check=True)


@pytest.fixture
def remote(tmp_path) -> Path:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    (seed / "README.md").write_text("vault\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "main")
    return bare


@pytest.fixture
def clone(tmp_path, remote) -> Path:
    path = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True)
    return path


@pytest.fixture
def vault(clone) -> Vault:
    return Vault(clone, FOLDER, author_name="Link2Note", author_email="bot@link2note")


def remote_files(remote: Path) -> list[str]:
    return git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def test_is_ready(vault, tmp_path):
    assert vault.is_ready()
    assert not Vault(tmp_path / "nope", FOLDER, "a", "b").is_ready()


async def test_v1_save_commits_and_pushes(vault, remote, clone):
    name = await vault.save("Мой конспект", "# Привет\nстрока\n", DAY)
    assert name == "2026-11-09 Мой конспект"
    assert f"{FOLDER}/2026-11-09 Мой конспект.md" in remote_files(remote)
    log = git(remote, "log", "-1", "--format=%an|%s", "main")
    assert log.strip() == "Link2Note|📥 Link2Note: Мой конспект"
    content = (clone / FOLDER / "2026-11-09 Мой конспект.md").read_bytes()
    assert content == "# Привет\nстрока\n".encode()  # LF, без \r\n


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ('a/b\\c:d*e?f"g<h>i|j#k^l[m]n', "abcdefghijklmn"),
        ("Конец с точкой...", "Конец с точкой"),
        ("  пробелы   ", "пробелы"),
        ("x" * 200, "x" * 80),
        ("Строка\nперенос\tтаб", "Строка перенос таб"),
        ("///", "Без названия"),
    ],
)
def test_v2_safe_filename(title, expected):
    assert safe_filename(title) == expected


async def test_v2_same_title_same_day_gets_suffix(vault, remote):
    first = await vault.save("Дубль", "1\n", DAY)
    second = await vault.save("Дубль", "2\n", DAY)
    third = await vault.save("Дубль", "3\n", DAY)
    assert (first, second, third) == (
        "2026-11-09 Дубль",
        "2026-11-09 Дубль (2)",
        "2026-11-09 Дубль (3)",
    )
    files = remote_files(remote)
    assert f"{FOLDER}/2026-11-09 Дубль (3).md" in files


async def test_v3_rebases_over_foreign_push(vault, remote, tmp_path):
    other = tmp_path / "phone"
    clone_repo(remote, other)
    (other / "Идея.md").write_text("с телефона\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "phone")
    git(other, "push", "-q")

    await vault.save("После телефона", "x\n", DAY)
    files = remote_files(remote)
    assert "Идея.md" in files
    assert f"{FOLDER}/2026-11-09 После телефона.md" in files


async def test_v4_push_rejected_raises_and_keeps_local_commit(vault, remote, clone):
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    with pytest.raises(VaultError) as exc:
        await vault.save("Не дошла", "x\n", DAY)
    assert "2026-11-09 Не дошла" in str(exc.value)
    assert exc.value.note_name == "2026-11-09 Не дошла"
    # Заметка закоммичена локально, репозиторий не застрял посреди rebase.
    assert "📥 Link2Note: Не дошла" in git(clone, "log", "-1", "--format=%s")
    assert not (clone / ".git" / "rebase-merge").exists()
    assert not (clone / ".git" / "rebase-apply").exists()
    assert git(clone, "status", "--porcelain").strip() == ""

    # Сеть «починилась» — следующая запись отправляет и старую заметку.
    hook.unlink()
    await vault.save("Следующая", "y\n", DAY)
    files = remote_files(remote)
    assert f"{FOLDER}/2026-11-09 Не дошла.md" in files
    assert f"{FOLDER}/2026-11-09 Следующая.md" in files


async def test_v4_rebase_conflict_aborted(vault, remote, clone, tmp_path):
    """Локальный неотправленный коммит конфликтует с чужим — rebase отменяется, не зависает."""
    # Локально правим README и коммитим, не отправляя.
    (clone / "README.md").write_text("локально\n", encoding="utf-8")
    git(clone, "commit", "-q", "-am", "local edit")
    # Тот же файл меняют «с телефона» и пушат.
    other = tmp_path / "phone"
    clone_repo(remote, other)
    (other / "README.md").write_text("с телефона\n", encoding="utf-8")
    git(other, "commit", "-q", "-am", "phone edit")
    git(other, "push", "-q")

    with pytest.raises(VaultError):
        await vault.save("Конфликт", "z\n", DAY)
    assert not (clone / ".git" / "rebase-merge").exists()
    assert not (clone / ".git" / "rebase-apply").exists()
    assert (clone / FOLDER / "2026-11-09 Конфликт.md").exists()


async def test_v4_unreachable_remote(vault, clone, tmp_path):
    git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(VaultError) as exc:
        await vault.save("Офлайн", "x\n", DAY)
    assert exc.value.note_name == "2026-11-09 Офлайн"
    assert (clone / FOLDER / "2026-11-09 Офлайн.md").exists()


async def test_v5_concurrent_saves(vault, remote):
    names = await asyncio.gather(
        vault.save("Первая", "1\n", DAY),
        vault.save("Вторая", "2\n", DAY),
        vault.save("Первая", "3\n", DAY),
    )
    assert sorted(names) == ["2026-11-09 Вторая", "2026-11-09 Первая", "2026-11-09 Первая (2)"]
    files = remote_files(remote)
    for n in names:
        assert f"{FOLDER}/{n}.md" in files


async def test_git_timeout_kills_hanging_push(vault, remote, clone, monkeypatch):
    """Зависший push не держит lock: таймаут → kill → VaultError задолго до конца «зависания».

    Хук спит 20 с; без таймаута и ограниченного ожидания после kill запись шла бы ≥ 60 с.
    """
    import time

    import digest.vault as vault_mod

    monkeypatch.setattr(vault_mod, "GIT_TIMEOUT_S", 1)
    monkeypatch.setattr(vault_mod, "KILL_WAIT_S", 0.5)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nsleep 20\nexit 1\n", encoding="utf-8", newline="\n")
    started = time.monotonic()
    with pytest.raises(VaultError) as exc:
        await vault.save("Зависла", "x\n", DAY)
    assert time.monotonic() - started < 15
    assert "нет ответа" in str(exc.value)
    assert not vault.lock.locked()
