"""Этап 5: новые переменные окружения и их проверка."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from config import Settings


def _s(**kw) -> Settings:
    return Settings(_env_file=None, bot_token="1:x", owner_id=1, **kw)


def test_defaults_git_backend():
    s = _s()
    assert s.vault_backend == "git"
    assert s.second_brain_api_url == "http://secondbrain-web:8000/api/v1"
    assert s.second_brain_timeout_s == 15
    assert s.outbox_max_age == timedelta(days=7)
    assert s.outbox_max_attempts == 200  # ~7 суток ежечасных повторов (финальная проверка)
    assert s.gemini_detail_model == "gemini-3.8-flash"
    assert s.dry_run is False


def test_api_requires_token():
    with pytest.raises(ValidationError, match="SECOND_BRAIN_API_TOKEN"):
        _s(vault_backend="api")


def test_api_requires_http_url():
    with pytest.raises(ValidationError, match="SECOND_BRAIN_API_URL"):
        _s(vault_backend="api", second_brain_api_token="t", second_brain_api_url="secondbrain")


def test_api_url_trailing_slash_stripped():
    s = _s(vault_backend="api", second_brain_api_token="t", second_brain_api_url="http://x/api/v1/")
    assert s.second_brain_api_url == "http://x/api/v1"


def test_unknown_backend_rejected():
    with pytest.raises(ValidationError):
        _s(vault_backend="ftp")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7d", timedelta(days=7)),
        ("12h", timedelta(hours=12)),
        ("30m", timedelta(minutes=30)),
        ("3", timedelta(days=3)),
        (2, timedelta(days=2)),
    ],
)
def test_outbox_max_age_formats(raw, expected):
    assert _s(outbox_max_age=raw).outbox_max_age == expected


def test_outbox_max_age_garbage():
    with pytest.raises(ValidationError, match="OUTBOX_MAX_AGE"):
        _s(outbox_max_age="неделя")


def test_token_not_in_repr(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("VAULT_BACKEND", "api")
    monkeypatch.setenv("SECOND_BRAIN_API_TOKEN", "sb-secret-123")
    s = Settings(_env_file=None)
    assert "sb-secret-123" not in repr(s) and "sb-secret-123" not in str(s.model_dump())
    assert s.second_brain_api_token.get_secret_value() == "sb-secret-123"


def test_env_bool_dry_run(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    assert Settings(_env_file=None).dry_run is True


def test_validation_error_never_shows_tokens(monkeypatch):
    """Ревью A+B #1: pydantic печатал input_value (с токенами) в ошибке model_validator."""
    monkeypatch.setenv("BOT_TOKEN", "123456:AAA-live-bot-secret")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("VAULT_BACKEND", "api")
    monkeypatch.setenv("SECOND_BRAIN_API_URL", "secondbrain-web:8000/api/v1")  # без http://
    monkeypatch.setenv("SECOND_BRAIN_API_TOKEN", "sb-live-secret-0123456789")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)
    text = str(err.value)
    assert "sb-live-secret" not in text and "live-bot-secret" not in text


def test_token_stripped_and_blank_rejected():
    s = _s(vault_backend="api", second_brain_api_token="  sb-x \n")
    assert s.second_brain_api_token.get_secret_value() == "sb-x"
    with pytest.raises(ValidationError, match="SECOND_BRAIN_API_TOKEN"):
        _s(vault_backend="api", second_brain_api_token="   ")


@pytest.mark.parametrize("raw", ["0", "0d", 0])
def test_outbox_max_age_zero_rejected(raw):
    with pytest.raises(ValidationError, match="OUTBOX_MAX_AGE"):
        _s(outbox_max_age=raw)


def test_timeout_positive():
    with pytest.raises(ValidationError, match="SECOND_BRAIN_TIMEOUT_S"):
        _s(second_brain_timeout_s=0)
