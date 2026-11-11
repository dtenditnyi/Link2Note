"""Все настройки бота — из переменных окружения / .env (pydantic-settings)."""

import re
from collections.abc import Mapping
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Дневная квота Gemini сбрасывается в полночь по тихоокеанскому времени.
QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")


_AGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhm]?)")
_AGE_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


class Settings(BaseSettings):
    # hide_input_in_errors: ошибка проверки не печатает входные значения (токены) в лог.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

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

    # Этап 5: «Подробнее» — более сильная модель (20 запросов/сутки); пусто — основная.
    gemini_detail_model: str = "gemini-3.8-flash"

    # Этап 5: куда писать заметки. git — свой клон (VAULT_REPO); api — сайт Second Brain.
    vault_backend: Literal["git", "api"] = "git"
    second_brain_api_url: str = "http://secondbrain-web:8000/api/v1"
    second_brain_api_token: SecretStr = SecretStr("")
    second_brain_timeout_s: float = 15
    outbox_max_age: timedelta = timedelta(days=7)  # «7d», «12h», «30m» или число дней
    outbox_max_attempts: int = 200  # ~7 суток ежечасных повторов

    # Без Telegram: проверка контейнера (healthcheck, очередь, база) с фиктивным токеном.
    dry_run: bool = False

    @field_validator("vault_repo", "cookies_file", mode="before")
    @classmethod
    def _empty_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("outbox_max_age", mode="before")
    @classmethod
    def _age(cls, value):
        if isinstance(value, int | float) and not isinstance(value, bool):
            return timedelta(days=value)
        if isinstance(value, str):
            m = _AGE_RE.fullmatch(value.strip().lower())
            if m is None:
                raise ValueError(
                    f"OUTBOX_MAX_AGE: «7d», «12h», «30m» или число дней, а не {value!r}"
                )
            amount, unit = float(m.group(1)), m.group(2) or "d"
            return timedelta(**{_AGE_UNITS[unit]: amount})
        return value

    @field_validator("second_brain_api_token", mode="before")
    @classmethod
    def _strip_token(cls, value):
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return value.strip() if isinstance(value, str) else value

    @field_validator("second_brain_api_url")
    @classmethod
    def _api_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _api_needs_token(self):
        if self.vault_backend == "api":
            if not self.second_brain_api_url.startswith(("http://", "https://")):
                raise ValueError("VAULT_BACKEND=api: SECOND_BRAIN_API_URL должен быть http(s)://…")
            if not self.second_brain_api_token.get_secret_value().strip():
                raise ValueError("VAULT_BACKEND=api: не задан SECOND_BRAIN_API_TOKEN")
        if self.outbox_max_attempts < 1:
            raise ValueError("OUTBOX_MAX_ATTEMPTS должен быть ≥ 1")
        if self.outbox_max_age <= timedelta(0):
            raise ValueError("OUTBOX_MAX_AGE должен быть больше нуля")
        if self.second_brain_timeout_s <= 0:
            raise ValueError("SECOND_BRAIN_TIMEOUT_S должен быть больше нуля")
        return self

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


def gemini_models_log(s: Settings, environ: Mapping[str, str]) -> list[str]:
    """Строки для лога при старте: какие модели Gemini реально в работе и откуда они.

    Переменная окружения процесса перекрывает .env (pydantic-settings) — например, старый
    GEMINI_MODEL, оставшийся в терминале VS Code.
    """
    if not s.gemini_fallback_model:
        fallback = "выключена"
    elif s.gemini_fallback_model == s.gemini_model:
        fallback = "выключена (как основная)"
    else:
        fallback = s.gemini_fallback_model
    lines = [f"Gemini: основная {s.gemini_model}, запасная {fallback}"]
    for var in ("GEMINI_MODEL", "GEMINI_FALLBACK_MODEL"):
        if var in environ:
            lines.append(
                f"{var}={environ[var]!r} взят из переменной окружения процесса (перекрывает .env)"
            )
    return lines


@lru_cache
def get_settings() -> Settings:
    return Settings()
