from pathlib import Path
from zoneinfo import ZoneInfo

from config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OWNER_ID", "555")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("TZ", "Europe/Bucharest")
    s = Settings(_env_file=None)
    assert s.bot_token == "123:abc"
    assert s.owner_id == 555
    assert s.timezone == ZoneInfo("Europe/Bucharest")


def test_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.delenv("TZ", raising=False)
    s = Settings(_env_file=None)
    assert s.whisper_model == "small"
    assert s.whisper_device == "cpu"
    assert s.whisper_compute_type == "int8"
    assert s.voice_max_seconds == 120
    assert s.tz == "Europe/Bucharest"
    assert s.gemini_model == "gemini-3.8-flash"


def test_extra_env_vars_ignored(monkeypatch):
    """В .env есть переменные будущих этапов (VAULT_REPO, COOKIES_FILE) — не падаем."""
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("VAULT_REPO", "/vault")
    Settings(_env_file=None)


def test_timezone_defined_only_in_config():
    """T4: строка часового пояса — только в config.py."""
    hits = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).parts
        if rel[0] in {".venv", "legacy", "tests"}:
            continue
        if "Europe/Bucharest" in path.read_text(encoding="utf-8"):
            hits.append(path.name)
    assert hits == ["config.py"]


def test_invalid_tz_fails_fast(monkeypatch):
    import pytest
    from pydantic import ValidationError

    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("TZ", "EET-2EEST,M3.5.0/3,M10.5.0/4")
    with pytest.raises(ValidationError, match="IANA"):
        Settings(_env_file=None)


def test_stage1_vault_and_digest_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.delenv("VAULT_REPO", raising=False)
    s = Settings(_env_file=None)
    assert s.vault_repo is None
    assert s.vault_folder == "03 - Resources/📥 Из ссылок"
    assert s.git_author_name == "Link2Note"
    assert s.git_author_email == "link2note@localhost"
    assert s.gemini_timeout_s == 300


def test_vault_repo_from_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("VAULT_REPO", "/vault")
    assert Settings(_env_file=None).vault_repo == Path("/vault")


def test_empty_vault_repo_is_none(monkeypatch):
    """VAULT_REPO= (пусто в .env) — значит «не настроено», а не текущая папка."""
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("VAULT_REPO", "")
    assert Settings(_env_file=None).vault_repo is None


def test_stage2_video_settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("COOKIES_FILE", "")
    s = Settings(_env_file=None)
    assert s.cookies_file is None
    assert s.video_max_seconds == 3600
    assert s.video_max_mb == 300
    monkeypatch.setenv("COOKIES_FILE", "/secrets/cookies.txt")
    assert Settings(_env_file=None).cookies_file == Path("/secrets/cookies.txt")
