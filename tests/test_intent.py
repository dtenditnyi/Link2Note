import json
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.intent import IntentError, IntentParser, ReminderIntent, build_system_prompt
from services.timeparse import Repeat
from tests.conftest import TZ


def fake_client(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=SimpleNamespace(text=text))
    return client


def parser(client):
    return IntentParser(api_key="k", model="gemini-test", tz=TZ, client=client)


def test_prompt_has_now_and_no_past_dates(now):
    """V7."""
    prompt = build_system_prompt(now)
    assert "2026-11-08 12:00" in prompt
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", prompt)
    assert dates, "в промпте должны быть примеры с датами"
    assert all(d >= "2026-11-08" for d in dates)


async def test_parse_ok(now):
    client = fake_client(
        {"task": "Выпить таблетки", "remind_at": "2026-11-08 12:02", "repeat": "none"}
    )
    r = await parser(client).parse("напомни выпить таблетки через 2 минуты", None, now)
    assert r.task == "Выпить таблетки"
    assert r.remind_at == datetime(2026, 11, 8, 12, 2, tzinfo=TZ)
    assert r.repeat is Repeat.NONE


async def test_request_uses_schema_model_and_context(now):
    client = fake_client({"task": "t", "remind_at": "2026-11-08 13:00", "repeat": "none"})
    await parser(client).parse("сделай это вечером", "https://eda.ru/pirog", now)
    kwargs = client.aio.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-test"
    assert "https://eda.ru/pirog" in str(kwargs["contents"])
    assert "сделай это вечером" in str(kwargs["contents"])
    cfg = kwargs["config"]
    assert cfg.response_schema is ReminderIntent
    assert cfg.response_mime_type == "application/json"
    assert cfg.temperature == 0


async def test_daily_in_past_today_rolls_forward(now):
    client = fake_client({"task": "зарядка", "remind_at": "2026-11-08 09:00", "repeat": "daily"})
    r = await parser(client).parse("каждый день в 9 зарядка", None, now)
    assert r.repeat is Repeat.DAILY
    assert r.remind_at == datetime(2026, 11, 9, 9, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    "payload",
    [
        "не json вообще",
        {"task": "x"},
        {"remind_at": "2026-11-08 13:00"},
        {"task": "x", "remind_at": "завтра"},
        {"task": "   ", "remind_at": "2026-11-08 13:00"},
        {"task": "x", "remind_at": "2026-11-08 13:00", "repeat": "hourly"},
    ],
)
async def test_bad_llm_output_raises(now, payload):
    """V5."""
    with pytest.raises(IntentError):
        await parser(fake_client(payload)).parse("...", None, now)


async def test_past_time_raises(now):
    client = fake_client({"task": "x", "remind_at": "2026-11-08 11:00", "repeat": "none"})
    with pytest.raises(IntentError, match="прошл"):
        await parser(client).parse("...", None, now)


async def test_empty_response_raises(now):
    client = fake_client("")
    client.aio.models.generate_content.return_value = SimpleNamespace(text=None)
    with pytest.raises(IntentError):
        await parser(client).parse("...", None, now)


def test_client_is_lazy(monkeypatch):
    """Создание парсера не создаёт клиента Gemini (нет ключа — бот всё равно стартует)."""
    import services.intent as mod

    created = []
    monkeypatch.setattr(mod.genai, "Client", lambda **kw: created.append(kw))
    IntentParser(api_key="k", model="m", tz=TZ)
    assert created == []


async def test_kind_label_in_request(now):
    """Подпись команды настраивается: «Текстовая команда» для свободного текста."""
    client = fake_client({"task": "t", "remind_at": "2026-11-08 13:00", "repeat": "none"})
    await parser(client).parse("напомни купить хлеб", None, now, kind="Текстовая команда")
    contents = str(client.aio.models.generate_content.call_args.kwargs["contents"])
    assert "Текстовая команда: напомни купить хлеб" in contents


async def test_default_kind_is_voice(now):
    client = fake_client({"task": "t", "remind_at": "2026-11-08 13:00", "repeat": "none"})
    await parser(client).parse("x", None, now)
    assert "Голосовая команда: x" in str(client.aio.models.generate_content.call_args.kwargs)


# --- Запасная модель (I1–I3) ------------------------------------------------------------------


def _intent_by_model(outcomes: dict, fallback="gemini-fallback"):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    calls: list[str] = []

    async def generate(*, model, contents, config):
        calls.append(model)
        out = outcomes[model]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out)

    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=generate)
    parser = IntentParser(
        api_key="k", model="gemini-main", tz=TZ, client=client, fallback_model=fallback
    )
    return parser, calls


def _api_err(code):
    from google.genai import errors

    body = {"error": {"code": code, "message": "m", "status": "S"}}
    return errors.ServerError(code, body) if code >= 500 else errors.ClientError(code, body)


def _ok_json(now):
    import json
    from datetime import timedelta

    at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    return json.dumps({"task": "Выпить воды", "remind_at": at, "repeat": "none"})


@pytest.mark.parametrize("code", [429, 500, 503])
async def test_i1_overloaded_uses_fallback(now, code):
    parser, calls = _intent_by_model(
        {"gemini-main": _api_err(code), "gemini-fallback": _ok_json(now)}
    )
    result = await parser.parse("напомни выпить воды через 5 минут", None, now)
    assert result.task == "Выпить воды"
    assert calls == ["gemini-main", "gemini-fallback"]


async def test_i2_client_error_no_fallback(now):
    from google.genai import errors

    parser, calls = _intent_by_model(
        {"gemini-main": _api_err(400), "gemini-fallback": _ok_json(now)}
    )
    with pytest.raises(errors.ClientError):
        await parser.parse("x", None, now)
    assert calls == ["gemini-main"]


async def test_i3_both_down_raises_fallback_error(now):
    from google.genai import errors

    parser, calls = _intent_by_model(
        {"gemini-main": _api_err(503), "gemini-fallback": _api_err(429)}
    )
    with pytest.raises(errors.APIError) as exc:
        await parser.parse("x", None, now)
    assert exc.value.code == 429
    assert calls == ["gemini-main", "gemini-fallback"]


@pytest.mark.parametrize("fallback", ["", "gemini-main"])
async def test_i3_no_fallback_single_call(now, fallback):
    from google.genai import errors

    parser, calls = _intent_by_model({"gemini-main": _api_err(503)}, fallback=fallback)
    with pytest.raises(errors.ServerError):
        await parser.parse("x", None, now)
    assert calls == ["gemini-main"]
