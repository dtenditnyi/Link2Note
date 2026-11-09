"""Все настройки бота — из переменных окружения / .env (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Дневная квота Gemini сбрасывается в полночь по тихоокеанскому времени.
QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_id: int

    gemini_api_key: str = ""
    # Free tier (09.11.2026): у Lite 500 запросов/сутки и 15/мин, у gemini-3.8-flash — 20/сутки.
    gemini_model: str = "gemini-3.5-flash-lite"
    # Одна попытка на запасной модели при 429/5xx; пусто или как основная — выключено.
    gemini_fallback_model: str = "gemini-3.1-flash-lite"

    tz: str = "Europe/Bucharest"
    db_path: Path = Path("data/link2note.db")

    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    voice_max_seconds: int = 120

    # Link2Note: конспекты и хранилище
    gemini_timeout_s: int = 300
    vault_repo: Path | None = None  # клон хранилища Obsidian; пусто — сохранять некуда
    vault_folder: str = "03 - Resources/📥 Из ссылок"
    git_author_name: str = "Link2Note"
    git_author_email: str = "link2note@localhost"

    # Этап 2: видео через yt-dlp
    cookies_file: Path | None = None  # cookies.txt (Netscape) для Instagram; пусто — без входа
    video_max_seconds: int = 3600
    video_max_mb: int = 300

    # Этап 3: Gemini не открыл видео → звук → Whisper (CPU); длиннее — не расшифровываем.
    whisper_fallback_max_seconds: int = 1800
    weekly_digest: bool = True  # вс 18:00 — список непрочитанных конспектов

    @field_validator("vault_repo", "cookies_file", mode="before")
    @classmethod
    def _empty_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise ValueError(
                f"TZ must be an IANA zone like Europe/Bucharest, got {value!r}"
            ) from err
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


@lru_cache
def get_settings() -> Settings:
    return Settings()
