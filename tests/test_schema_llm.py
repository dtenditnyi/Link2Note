import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google import genai
from google.genai import _transformers, types

from digest.errors import DigestError
from digest.llm import ALLOWED_TAGS, SYSTEM, DigestLLM, clean
from digest.schema import Digest

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

MINIMAL = {
    "title": "Заголовок",
    "emoji": "⌨️",
    "tldr": "Суть.",
    "key_points": ["Пункт"],
    "tags": ["dev"],
    "read_minutes": 2,
    "confidence": "high",
}


def _llm(response_text: str | None):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text=response_text, parsed=None)
    )
    return DigestLLM(api_key="k", model="gemini-test", timeout_s=300, client=client), client


def _call_kwargs(client):
    return client.aio.models.generate_content.call_args.kwargs


async def test_l1_request_shape():
    llm, client = _llm(json.dumps(MINIMAL))
    await llm.digest_youtube(URL, "сделай упор на хоткеи")

    kw = _call_kwargs(client)
    assert kw["model"] == "gemini-test"
    parts = kw["contents"].parts
    assert parts[0].file_data.file_uri == URL
    assert "сделай упор на хоткеи" in parts[1].text

    cfg: types.GenerateContentConfig = kw["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is Digest
    assert cfg.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert cfg.temperature == 0.2
    assert cfg.system_instruction == SYSTEM
    http = cfg.http_options
    assert http is not None and http.retry_options is not None
    assert http.timeout == 300_000
    assert set(http.retry_options.http_status_codes or []) >= {429, 503}
    assert http.retry_options.attempts == 3


async def test_l1_no_hint_no_garbage():
    llm, client = _llm(json.dumps(MINIMAL))
    await llm.digest_youtube(URL)
    text = _call_kwargs(client)["contents"].parts[1].text
    assert "Подсказка" not in text
    assert text.strip()


def test_system_prompt_lists_allowed_tags():
    for tag in ALLOWED_TAGS:
        assert tag in SYSTEM


async def test_l2_tags_filtered():
    data = {**MINIMAL, "tags": ["dev", "кулинария", "ai", "homelab", "obsidian"]}
    llm, _ = _llm(json.dumps(data))
    d = (await llm.digest_youtube(URL)).digest
    assert d.tags == ["dev", "ai", "homelab"]


async def test_l2_no_valid_tags_gives_personal():
    data = {**MINIMAL, "tags": ["unknown"]}
    llm, _ = _llm(json.dumps(data))
    assert (await llm.digest_youtube(URL)).digest.tags == ["personal"]


def test_clean_tags_case_and_dupes():
    d = Digest.model_validate({**MINIMAL, "tags": ["Dev", "dev", " AI "]})
    assert clean(d).tags == ["dev", "ai"]


def test_clean_read_minutes_at_least_one():
    d = Digest.model_validate({**MINIMAL, "read_minutes": 0})
    assert clean(d).read_minutes == 1


@pytest.mark.parametrize("bad", [None, "", "not json", '{"title": "x"}'])
async def test_l3_bad_response_raises_digest_error(bad):
    llm, _ = _llm(bad)
    with pytest.raises(DigestError) as exc:
        await llm.digest_youtube(URL)
    assert any("а" <= ch <= "я" for ch in str(exc.value).lower())  # по-русски


async def test_l3_optional_lists_default_empty():
    llm, _ = _llm(json.dumps(MINIMAL))
    d = (await llm.digest_youtube(URL)).digest
    assert d.steps == d.shortcuts == d.code == d.resources == []
    assert d.action_items == d.timestamps == []


def test_confidence_restricted():
    with pytest.raises(ValueError):
        Digest.model_validate({**MINIMAL, "confidence": "very"})


def test_l4_schema_sent_to_gemini_has_no_defaults():
    schema = _transformers.t_schema(genai.Client(api_key="x")._api_client, Digest)
    assert schema is not None
    dumped = schema.model_dump(exclude_none=True)
    assert "default" not in json.dumps(dumped, default=str)
    assert set(dumped["required"]) == {
        "title",
        "emoji",
        "tldr",
        "key_points",
        "tags",
        "read_minutes",
        "confidence",
    }


# --- Ошибки Gemini → понятный DigestError (после ревью) ---------------------------------


def _llm_raising(exc: Exception):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=exc)
    return DigestLLM(api_key="k", model="m", client=client)


async def test_gemini_quota_429_is_digest_error():
    from google.genai import errors

    exc = errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    with pytest.raises(DigestError, match="[Лл]имит"):
        await _llm_raising(exc).digest_youtube(URL)


async def test_gemini_400_is_digest_error():
    from google.genai import errors

    exc = errors.ClientError(400, {"error": {"message": "bad video", "status": "INVALID_ARGUMENT"}})
    with pytest.raises(DigestError, match="не смог"):
        await _llm_raising(exc).digest_youtube(URL)


async def test_gemini_5xx_is_digest_error():
    from google.genai import errors

    exc = errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    with pytest.raises(DigestError, match="недоступен"):
        await _llm_raising(exc).digest_youtube(URL)


# --- Этап 2: видео-файл через Files API, текст статьи, теги -----------------------------


def _file(state: str, name="files/abc", uri="https://gen.googleapis.com/files/abc"):
    return SimpleNamespace(name=name, uri=uri, mime_type="video/mp4", state=types.FileState(state))


MINIMAL_JSON = json.dumps(MINIMAL)


def _video_llm(states: list[str], response=MINIMAL_JSON, *, timeout_s=5):
    client = MagicMock()
    client.aio.files.upload = AsyncMock(return_value=_file(states[0]))
    client.aio.files.get = AsyncMock(side_effect=[_file(s) for s in states[1:]])
    client.aio.files.delete = AsyncMock()
    client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text=response, parsed=None)
    )
    llm = DigestLLM(
        api_key="k",
        model="gemini-test",
        client=client,
        poll_interval_s=0,
        processing_timeout_s=timeout_s,
    )
    return llm, client


async def test_l5_video_file_flow(tmp_path):
    video = tmp_path / "C1abc.mp4"
    video.write_bytes(b"x")
    llm, client = _video_llm(["PROCESSING", "PROCESSING", "ACTIVE"])
    d = (await llm.digest_video_file(video, "video/mp4", "упор на хоткеи")).digest
    assert d.title == "Заголовок"

    up = client.aio.files.upload.call_args.kwargs
    assert Path(up["file"]) == video
    cfg = up["config"]
    mime = cfg["mime_type"] if isinstance(cfg, dict) else cfg.mime_type
    assert mime == "video/mp4"
    assert client.aio.files.get.await_count == 2

    kw = _call_kwargs(client)
    parts = kw["contents"].parts
    assert parts[0].file_data.file_uri == "https://gen.googleapis.com/files/abc"
    assert parts[0].file_data.mime_type == "video/mp4"
    assert "упор на хоткеи" in parts[1].text
    assert kw["config"].response_schema is Digest
    client.aio.files.delete.assert_awaited_once_with(name="files/abc")


async def test_l5_failed_state_still_deletes(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    llm, client = _video_llm(["PROCESSING", "FAILED"])
    with pytest.raises(DigestError, match="не смог обработать"):
        await llm.digest_video_file(video, "video/mp4")
    client.aio.models.generate_content.assert_not_awaited()
    client.aio.files.delete.assert_awaited_once_with(name="files/abc")


async def test_l5_processing_timeout_still_deletes(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    llm, client = _video_llm(["PROCESSING"] + ["PROCESSING"] * 50, timeout_s=0)
    with pytest.raises(DigestError, match="слишком долго"):
        await llm.digest_video_file(video, "video/mp4")
    client.aio.files.delete.assert_awaited_once_with(name="files/abc")


async def test_l5_delete_failure_is_not_fatal(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    llm, client = _video_llm(["ACTIVE"])
    client.aio.files.delete = AsyncMock(side_effect=RuntimeError("network"))
    d = (await llm.digest_video_file(video, "video/mp4")).digest
    assert d.title == "Заголовок"


async def test_l5_bad_response_after_upload_still_deletes(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    llm, client = _video_llm(["ACTIVE"], response="not json")
    with pytest.raises(DigestError):
        await llm.digest_video_file(video, "video/mp4")
    client.aio.files.delete.assert_awaited_once()


async def test_l6_text_request():
    llm, client = _llm(json.dumps(MINIMAL))
    long_text = "абв " * 100_000  # 400 000 символов
    await llm.digest_text(long_text, title="Советы по VS Code", hint="кратко")
    prompt = _call_kwargs(client)["contents"].parts[0].text
    assert "Советы по VS Code" in prompt
    assert "кратко" in prompt
    assert "абв" in prompt
    assert len(prompt) < 200_000 + 1_000
    assert _call_kwargs(client)["config"].response_schema is Digest


async def _call(method: str, llm: DigestLLM, tmp_path: Path):
    if method == "youtube":
        return await llm.digest_youtube(URL)
    if method == "text":
        return await llm.digest_text("текст", title="t")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    return await llm.digest_video_file(video, "video/mp4")


@pytest.mark.parametrize("method", ["youtube", "text", "video"])
async def test_l7_errors_mapped_for_all_methods(method, tmp_path):
    from google.genai import errors

    quota = errors.ClientError(429, {"error": {"message": "q", "status": "RESOURCE_EXHAUSTED"}})
    down = errors.ServerError(503, {"error": {"message": "o", "status": "UNAVAILABLE"}})
    for exc, match in ((quota, "[Лл]имит"), (down, "недоступен")):
        llm, client = _video_llm(["ACTIVE"])
        client.aio.models.generate_content = AsyncMock(side_effect=exc)
        with pytest.raises(DigestError, match=match):
            await _call(method, llm, tmp_path)


def test_l8_fun_tag_and_descriptions():
    assert "fun" in ALLOWED_TAGS
    lines = SYSTEM.splitlines()
    for tag in ALLOWED_TAGS:
        assert any(line.strip().startswith(f"- {tag} —") for line in lines), tag
    books = next(line for line in lines if line.strip().startswith("- books —"))
    assert "только" in books and "книг" in books


def test_l8_clean_keeps_correct_tags():
    assert clean(Digest.model_validate({**MINIMAL, "tags": ["fun"]})).tags == ["fun"]
    assert clean(Digest.model_validate({**MINIMAL, "tags": ["dev", "ai"]})).tags == ["dev", "ai"]


def test_prompt_is_not_video_only():
    """Промпт общий для видео и статей: правила про «материал», а не только «видео»."""
    assert "материал" in SYSTEM
    assert "timestamps" in SYSTEM and "стать" in SYSTEM  # у статей таймкодов нет


# --- Запасная модель (G1–G7) ------------------------------------------------------------------

from google.genai import errors as genai_errors  # noqa: E402

from digest.errors import GeminiBusy  # noqa: E402
from digest.llm import Digested  # noqa: E402


def _err(code: int, *, daily: bool = False):
    if code == 429:
        quota = (
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
            if daily
            else ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
        )
        body = {
            "error": {
                "code": 429,
                "message": "Resource has been exhausted",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaMetric": "x", "quotaId": quota}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "33s"},
                ],
            }
        }
        return genai_errors.ClientError(429, body)
    body = {"error": {"code": code, "message": "The model is overloaded", "status": "X"}}
    if code >= 500:
        return genai_errors.ServerError(code, body)
    return genai_errors.ClientError(code, body)


def _by_model(outcomes: dict):
    """generate_content, отвечающий по имени модели: исключение или JSON-текст."""
    calls: list[str] = []

    async def generate(*, model, contents, config):
        calls.append(model)
        out = outcomes[model]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out, parsed=None)

    return generate, calls


def _fb_llm(outcomes: dict, fallback="gemini-fallback"):
    generate, calls = _by_model(outcomes)
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=generate)
    client.aio.files.upload = AsyncMock(return_value=_file("ACTIVE"))
    client.aio.files.delete = AsyncMock()
    llm = DigestLLM(
        api_key="k",
        model="gemini-main",
        client=client,
        fallback_model=fallback,
        poll_interval_s=0,
    )
    return llm, client, calls


async def test_g1_main_ok_no_fallback():
    llm, _, calls = _fb_llm({"gemini-main": MINIMAL_JSON})
    res = await llm.digest_youtube(URL)
    assert isinstance(res, Digested)
    assert res.model == "gemini-main"
    assert res.digest.title == "Заголовок"
    assert calls == ["gemini-main"]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
async def test_g2_g3_overloaded_main_uses_fallback(code):
    llm, client, calls = _fb_llm({"gemini-main": _err(code), "gemini-fallback": MINIMAL_JSON})
    res = await llm.digest_youtube(URL, "упор")
    assert calls == ["gemini-main", "gemini-fallback"]
    assert res.model == "gemini-fallback"
    first, second = client.aio.models.generate_content.call_args_list
    assert first.kwargs["contents"] == second.kwargs["contents"]


async def test_g2_video_file_uploaded_once_for_fallback(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    llm, client, calls = _fb_llm({"gemini-main": _err(503), "gemini-fallback": MINIMAL_JSON})
    res = await llm.digest_video_file(video, "video/mp4")
    assert res.model == "gemini-fallback"
    assert calls == ["gemini-main", "gemini-fallback"]
    client.aio.files.upload.assert_awaited_once()
    client.aio.files.delete.assert_awaited_once()


async def test_g2_text_fallback():
    llm, _, calls = _fb_llm({"gemini-main": _err(503), "gemini-fallback": MINIMAL_JSON})
    res = await llm.digest_text("текст статьи", title="t")
    assert res.model == "gemini-fallback" and calls == ["gemini-main", "gemini-fallback"]


@pytest.mark.parametrize("code", [400, 403, 404])
async def test_g4_client_error_no_fallback(code):
    llm, _, calls = _fb_llm({"gemini-main": _err(code), "gemini-fallback": MINIMAL_JSON})
    with pytest.raises(DigestError) as exc:
        await llm.digest_youtube(URL)
    assert not isinstance(exc.value, GeminiBusy)
    assert calls == ["gemini-main"]


async def test_g5_both_overloaded_is_busy():
    llm, _, calls = _fb_llm({"gemini-main": _err(503), "gemini-fallback": _err(503)})
    with pytest.raises(GeminiBusy) as exc:
        await llm.digest_youtube(URL)
    assert exc.value.daily is False
    assert calls == ["gemini-main", "gemini-fallback"]


async def test_g5_both_daily_quota():
    llm, _, _ = _fb_llm(
        {"gemini-main": _err(429, daily=True), "gemini-fallback": _err(429, daily=True)}
    )
    with pytest.raises(GeminiBusy) as exc:
        await llm.digest_youtube(URL)
    assert exc.value.daily is True


async def test_g5_daily_and_503_is_not_daily():
    llm, _, _ = _fb_llm({"gemini-main": _err(429, daily=True), "gemini-fallback": _err(503)})
    with pytest.raises(GeminiBusy) as exc:
        await llm.digest_youtube(URL)
    assert exc.value.daily is False


@pytest.mark.parametrize("fallback", ["", "gemini-main"])
async def test_g6_no_fallback_single_call(fallback):
    llm, _, calls = _fb_llm({"gemini-main": _err(503)}, fallback=fallback)
    with pytest.raises(GeminiBusy):
        await llm.digest_youtube(URL)
    assert calls == ["gemini-main"]


async def test_g7_fallback_client_error_is_plain_digest_error():
    llm, _, _ = _fb_llm({"gemini-main": _err(503), "gemini-fallback": _err(400)})
    with pytest.raises(DigestError) as exc:
        await llm.digest_youtube(URL)
    assert not isinstance(exc.value, GeminiBusy)


async def test_m2_fallback_logged(caplog):
    import logging

    caplog.set_level(logging.INFO)
    llm, _, _ = _fb_llm({"gemini-main": _err(503), "gemini-fallback": MINIMAL_JSON})
    await llm.digest_youtube(URL)
    assert "gemini-fallback" in caplog.text and "503" in caplog.text


def test_is_overloaded_and_daily():
    from services.gemini_errors import is_daily_quota, is_overloaded

    assert is_overloaded(_err(503)) and is_overloaded(_err(429)) and is_overloaded(_err(500))
    assert not is_overloaded(_err(400)) and not is_overloaded(RuntimeError("x"))
    assert is_daily_quota(_err(429, daily=True))
    assert not is_daily_quota(_err(429)) and not is_daily_quota(_err(503))


# --- После ревью: 500 не «перегружен» для откладывания -------------------------------------


def test_sdk_retries_only_overload_codes():
    from digest.llm import RETRY_STATUS

    assert sorted(RETRY_STATUS) == [429, 503]


@pytest.mark.parametrize("code", [500, 502, 504])
async def test_g8_server_errors_try_fallback_but_do_not_postpone(code):
    llm, _, calls = _fb_llm({"gemini-main": _err(code), "gemini-fallback": _err(code)})
    with pytest.raises(DigestError) as exc:
        await llm.digest_youtube(URL)
    assert not isinstance(exc.value, GeminiBusy)  # 500 часто постоянная — не откладываем
    assert "недоступен" in str(exc.value)
    assert calls == ["gemini-main", "gemini-fallback"]


async def test_g8_mixed_503_and_500_postpones():
    llm, _, _ = _fb_llm({"gemini-main": _err(500), "gemini-fallback": _err(503)})
    with pytest.raises(GeminiBusy):
        await llm.digest_youtube(URL)
