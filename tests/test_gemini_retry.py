"""Устойчивость к недоступности Gemini (docs/specs/gemini-resilience.md, G1–G8).

G1–G4 — настоящий genai.Client с подменённым httpx-транспортом: проверяем именно встроенные
ретраи SDK (HttpRetryOptions), без сети. G5–G8 — логика запасной модели на фейковом клиенте.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google import genai
from google.genai import errors, types

from services.intent import IntentError, IntentParser, LLMUnavailableError, is_transient
from tests.conftest import TZ

OK_PAYLOAD = {"task": "Выпить воды", "remind_at": "2026-11-08 12:02", "repeat": "none"}


def _ok_response() -> httpx.Response:
    body = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": json.dumps(OK_PAYLOAD)}]},
                "finishReason": "STOP",
            }
        ]
    }
    return httpx.Response(200, json=body)


def _error_response(code: int, status: str) -> httpx.Response:
    return httpx.Response(
        code, json={"error": {"code": code, "message": "high demand", "status": status}}
    )


class Script:
    """Транспорт: отдаёт ответы по очереди и запоминает запрошенные модели."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.models: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.models.append(request.url.path.rsplit("/", 1)[-1].split(":")[0])
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def real_parser(script: Script, **kwargs) -> IntentParser:
    client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(script))
        ),
    )
    return IntentParser(
        api_key="test-key",
        model="main-model",
        tz=TZ,
        client=client,
        retry_initial_delay=0.01,
        retry_max_delay=0.02,
        **kwargs,
    )


# --- G1–G4: встроенные ретраи SDK ----------------------------------------------------


@pytest.mark.parametrize(("code", "status"), [(503, "UNAVAILABLE"), (429, "RESOURCE_EXHAUSTED")])
async def test_transient_errors_retried_then_ok(now, code, status):
    """G1 (503) и G2 (429): две ошибки, потом успех — 3 запроса, результат получен."""
    script = Script(_error_response(code, status), _error_response(code, status), _ok_response())
    r = await real_parser(script).parse("напомни выпить воды через 2 минуты", None, now)
    assert r.task == "Выпить воды"
    assert len(script.models) == 3
    assert set(script.models) == {"main-model"}


async def test_timeout_retried(now):
    """G3."""
    script = Script(httpx.ReadTimeout("slow"), _ok_response())
    r = await real_parser(script).parse("...", None, now)
    assert r.task == "Выпить воды"
    assert len(script.models) == 2


@pytest.mark.parametrize(
    ("code", "status"), [(400, "INVALID_ARGUMENT"), (403, "PERMISSION_DENIED")]
)
async def test_client_errors_not_retried(now, code, status):
    """G4: неверный запрос / ключ — не повторяем и не считаем недоступностью."""
    script = Script(_error_response(code, status), _ok_response())
    with pytest.raises(errors.ClientError):
        await real_parser(script).parse("...", None, now)
    assert len(script.models) == 1


async def test_all_attempts_fail_raises_unavailable(now):
    script = Script(*[_error_response(503, "UNAVAILABLE") for _ in range(3)])
    with pytest.raises(LLMUnavailableError, match="503"):
        await real_parser(script).parse("...", None, now)
    assert len(script.models) == 3


async def test_fallback_model_through_real_client(now):
    """G6 на настоящем клиенте: 3×503 на основной, затем запасная модель."""
    script = Script(*[_error_response(503, "UNAVAILABLE") for _ in range(3)], _ok_response())
    r = await real_parser(script, fallback_model="backup-model").parse("...", None, now)
    assert r.task == "Выпить воды"
    assert script.models == ["main-model"] * 3 + ["backup-model"]


def test_retry_options_in_request_config(now):
    """Паузы нарастают: экспоненциальный рост, ограниченный max_delay; таймаут из настроек."""
    p = IntentParser(api_key="k", model="m", tz=TZ, timeout_s=20)
    cfg = p._config(now, attempts=2)
    ro = cfg.http_options.retry_options
    assert (ro.attempts, ro.initial_delay, ro.exp_base) == (2, 1.0, 2.0)
    assert ro.max_delay >= ro.initial_delay
    assert {408, 429, 500, 502, 503, 504} <= set(ro.http_status_codes)
    assert cfg.http_options.timeout == 20_000


# --- G5–G8: этапы и запасная модель (фейковый клиент) --------------------------------


def _server_error(code=503, status="UNAVAILABLE"):
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"code": code, "message": "high demand", "status": status}})


def fake_parser(side_effect, fallback_model=""):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=side_effect)
    p = IntentParser(api_key="k", model="main", tz=TZ, client=client, fallback_model=fallback_model)
    return p, client.aio.models.generate_content


def _ok():
    return SimpleNamespace(text=json.dumps(OK_PAYLOAD))


async def test_on_retry_busy_called_once_after_first_failure(now):
    """G5: первая попытка без ретраев → «busy» → вторая с ретраями SDK."""
    p, gen = fake_parser([_server_error(), _ok()])
    stages = []

    async def on_retry(stage):
        stages.append(stage)

    r = await p.parse("...", None, now, on_retry=on_retry)
    assert r.task == "Выпить воды"
    assert stages == ["busy"]
    attempts = [c.kwargs["config"].http_options.retry_options.attempts for c in gen.call_args_list]
    assert attempts == [1, 2]


async def test_no_retry_hook_when_first_attempt_ok(now):
    p, gen = fake_parser([_ok()])
    on_retry = AsyncMock()
    await p.parse("...", None, now, on_retry=on_retry)
    on_retry.assert_not_called()
    assert gen.call_count == 1


async def test_fallback_model_used_after_exhaustion(now):
    """G6."""
    p, gen = fake_parser([_server_error(), _server_error(), _ok()], fallback_model="backup")
    stages = []

    async def on_retry(stage):
        stages.append(stage)

    r = await p.parse("...", None, now, on_retry=on_retry)
    assert r.task == "Выпить воды"
    assert [c.kwargs["model"] for c in gen.call_args_list] == ["main", "main", "backup"]
    assert stages == ["busy", "fallback"]
    assert gen.call_args_list[2].kwargs["config"].http_options.retry_options.attempts == 1


async def test_no_fallback_when_not_configured(now):
    """G7."""
    p, gen = fake_parser([_server_error(), _server_error()])
    with pytest.raises(LLMUnavailableError):
        await p.parse("...", None, now)
    assert gen.call_count == 2


async def test_fallback_also_unavailable(now):
    """G8."""
    p, _ = fake_parser([_server_error(), _server_error(), _server_error(429, "X")], "backup")
    with pytest.raises(LLMUnavailableError):
        await p.parse("...", None, now)


async def test_429_goes_through_same_path(now):
    p, gen = fake_parser([_server_error(429, "RESOURCE_EXHAUSTED"), _ok()])
    r = await p.parse("...", None, now)
    assert r.task == "Выпить воды"
    assert gen.call_count == 2


async def test_bad_answer_still_intent_error(now):
    """Gemini ответил, но не понял — это не недоступность."""
    p, _ = fake_parser([SimpleNamespace(text="не json")])
    with pytest.raises(IntentError):
        await p.parse("...", None, now)


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (_server_error(503), True),
        (_server_error(500, "INTERNAL"), True),
        (_server_error(504, "DEADLINE_EXCEEDED"), True),
        (_server_error(429, "RESOURCE_EXHAUSTED"), True),
        (_server_error(408, "TIMEOUT"), True),
        (httpx.ReadTimeout("x"), True),
        (httpx.ConnectError("x"), True),
        (_server_error(400, "INVALID_ARGUMENT"), False),
        (_server_error(403, "PERMISSION_DENIED"), False),
        (ValueError("x"), False),
    ],
)
def test_is_transient(err, expected):
    assert is_transient(err) is expected


# --- По итогам ревью ----------------------------------------------------------------


def test_production_client_uses_httpx_like_tests():
    """Без своего транспорта SDK выбрал бы aiohttp; форсируем httpx — путь как в тестах."""
    client = IntentParser(api_key="k", model="m", tz=TZ)._get_client()
    assert client._api_client._use_aiohttp() is False


def test_aiohttp_connection_errors_are_transient():
    import aiohttp

    assert is_transient(aiohttp.ServerDisconnectedError())
    assert is_transient(aiohttp.ClientOSError())


async def test_failing_status_hook_does_not_abort_retries(now):
    """Сбой правки статуса в Telegram не должен отменять ретраи и запасную модель."""
    p, gen = fake_parser([_server_error(), _server_error(), _ok()], fallback_model="backup")

    async def broken_hook(stage):
        raise RuntimeError("message is not modified")

    r = await p.parse("...", None, now, on_retry=broken_hook)
    assert r.task == "Выпить воды"
    assert [c.kwargs["model"] for c in gen.call_args_list] == ["main", "main", "backup"]
