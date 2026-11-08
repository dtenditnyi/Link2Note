"""Все настройки бота — из переменных окружения / .env (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_id: int

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    tz: str = "Europe/Bucharest"
    db_path: Path = Path("data/link2note.db")

    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    voice_max_seconds: int = 120

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
