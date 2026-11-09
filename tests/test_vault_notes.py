# ruff: noqa: F811 — фикстуры remote/clone/vault импортированы из test_vault и
# подставляются pytest по имени параметра.
"""Этап 3: изменение заметок в хранилище — прочитано, замена, удаление, список непрочитанных."""

from datetime import date

import pytest

from digest.errors import NoteNotFound, VaultError
from digest.vault import UnreadNote
from tests.test_vault import (  # noqa: F401 — фикстуры
    DAY,
    FOLDER,
    clone,
    clone_repo,
    git,
    remote,
    remote_files,
    vault,
)

URL = "https://www.youtube.com/watch?v=abc"


def note_md(
    title="Хоткеи", url=URL, status="unread", day="2026-11-09", minutes=3, body="Текст\n"
) -> str:
    return (
        "---\n"
        "type: digest\n"
        f"status: {status}\n"
        "source: youtube\n"
        f'url: "{url}"\n'
        f"date_added: {day}\n"
        f"read_minutes: {minutes}\n"
        "confidence: high\n"
        "tags: [dev]\n"
        "---\n"
        f"# ⌨️ {title}\n\n"
        f"{body}"
    )


def remote_text(remote, rel: str) -> str:
    return git(remote, "show", f"main:{rel}")


async def test_b2_set_status_read_only_frontmatter(vault, remote):
    body = "В теле тоже есть строка\nstatus: unread\n"
    name = await vault.save("Хоткеи", note_md(body=body), DAY)
    got = await vault.set_status(name, URL, "read")
    assert got == name
    text = remote_text(remote, f"{FOLDER}/{name}.md")
    assert text == note_md(status="read", body=body)  # тело не тронуто
    log = git(remote, "log", "-1", "--format=%an|%s", "main").strip()
    assert log == f"Link2Note|✅ Link2Note: прочитано {name}"


async def test_b2_set_status_twice_no_empty_commit(vault, remote):
    name = await vault.save("Хоткеи", note_md(), DAY)
    await vault.set_status(name, URL, "read")
    before = git(remote, "rev-parse", "main")
    await vault.set_status(name, URL, "read")
    assert git(remote, "rev-parse", "main") == before


async def test_b6_renamed_note_found_by_url(vault, clone, remote, tmp_path):
    name = await vault.save("Хоткеи", note_md(), DAY)
    # Я переименовал и переместил заметку в Obsidian на другом устройстве.
    other = tmp_path / "other"
    clone_repo(remote, other)
    git(other, "mv", f"{FOLDER}/{name}.md", "Мои хоткеи.md")
    git(other, "commit", "-q", "-m", "rename")
    git(other, "push", "-q")

    got = await vault.set_status(name, URL, "read")
    assert got == "Мои хоткеи"
    assert "status: read" in remote_text(remote, "Мои хоткеи.md")


async def test_b6_missing_note_raises(vault):
    await vault.save("Другая", note_md(url="https://other"), DAY)
    with pytest.raises(NoteNotFound):
        await vault.set_status("2026-11-09 Нет такой", URL, "read")
    assert issubclass(NoteNotFound, VaultError)


async def test_b4_replace_keeps_file_name(vault, remote):
    name = await vault.save("Хоткеи", note_md(), DAY)
    new = note_md(title="Хоткеи подробно", body="Много текста\n")
    got = await vault.replace(name, URL, new)
    assert got == name
    files = [f for f in remote_files(remote) if f.startswith(FOLDER)]
    assert files == [f"{FOLDER}/{name}.md"]
    assert remote_text(remote, f"{FOLDER}/{name}.md") == new
    log = git(remote, "log", "-1", "--format=%s", "main").strip()
    assert log == f"🔁 Link2Note: подробнее {name}"


async def test_b3_delete_removes_file(vault, remote, clone):
    name = await vault.save("Хоткеи", note_md(), DAY)
    await vault.delete(name, URL)
    assert f"{FOLDER}/{name}.md" not in remote_files(remote)
    assert not (clone / FOLDER / f"{name}.md").exists()
    log = git(remote, "log", "-1", "--format=%s", "main").strip()
    assert log == f"🗑 Link2Note: удалено {name}"


async def test_b3_delete_missing_raises(vault):
    with pytest.raises(NoteNotFound):
        await vault.delete("2026-11-09 Нет", URL)


async def test_d3_unread_sorted_oldest_first(vault, clone, remote, tmp_path):
    await vault.save("Новая", note_md("Новая", url="https://n", day="2026-11-08"), DAY)
    await vault.save("Старая", note_md("Старая", url="https://s", day="2026-11-01", minutes=5), DAY)
    name = await vault.save("Прочитана", note_md("Прочитана", url="https://r"), DAY)
    await vault.set_status(name, "https://r", "read")
    # Не конспект (нет type: digest) и заметка, отмеченная прочитанной в Obsidian (через pull).
    other = tmp_path / "other"
    clone_repo(remote, other)
    (other / FOLDER / "Просто заметка.md").write_text("# Моя\nstatus: unread\n", encoding="utf-8")
    novaya = other / FOLDER / "2026-11-09 Новая.md"
    novaya.write_text(
        novaya.read_text(encoding="utf-8").replace("status: unread", "status: read"),
        encoding="utf-8",
    )
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "obsidian")
    git(other, "push", "-q")

    notes = await vault.unread()
    assert notes == [
        UnreadNote(
            name="2026-11-09 Старая",
            title="⌨️ Старая",
            date_added=date(2026, 11, 1),
            read_minutes=5,
        )
    ]


async def test_d3_unread_empty_folder(vault):
    assert await vault.unread() == []


# --- Ревью: две заметки с одним url («заново») -------------------------------------------


async def test_r1_url_fallback_skips_other_known_notes(vault, remote):
    """A удалена в Obsidian; по url нашлась бы B — но B принадлежит другой строке notes."""
    a = await vault.save("Хоткеи", note_md(), DAY)
    b = await vault.save("Хоткеи", note_md(body="Вторая\n"), DAY)
    assert a != b
    git(vault.repo, "rm", "-q", f"{FOLDER}/{a}.md")
    git(vault.repo, "commit", "-q", "-m", "rm a")
    with pytest.raises(NoteNotFound):
        await vault.delete(a, URL, skip={b})
    assert f"{FOLDER}/{b}.md" in remote_files(remote)


async def test_r1_url_fallback_ambiguous_raises(vault, clone, remote, tmp_path):
    await vault.save("A", note_md(), DAY)
    await vault.save("B", note_md(), DAY)
    with pytest.raises(NoteNotFound):
        await vault.set_status("2026-11-09 Нет", URL, "read")


async def test_r6_commit_failure_restores_file(vault, clone, monkeypatch):
    name = await vault.save("Хоткеи", note_md(), DAY)
    real = vault._git

    async def failing(*args):
        if args[0] == "commit":
            from digest.vault import _GitFailed

            raise _GitFailed("boom")
        return await real(*args)

    monkeypatch.setattr(vault, "_git", failing)
    with pytest.raises(VaultError) as info:
        await vault.delete(name, URL)
    assert info.value.note_name is None  # ничего не закоммичено
    assert (clone / FOLDER / f"{name}.md").exists()
    assert git(clone, "status", "--porcelain").strip() == ""
