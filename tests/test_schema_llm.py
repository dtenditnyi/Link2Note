import json
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
    d = await llm.digest_youtube(URL)
    assert d.tags == ["dev", "ai", "homelab"]


async def test_l2_no_valid_tags_gives_personal():
    data = {**MINIMAL, "tags": ["unknown"]}
    llm, _ = _llm(json.dumps(data))
    assert (await llm.digest_youtube(URL)).tags == ["personal"]


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
    d = await llm.digest_youtube(URL)
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
