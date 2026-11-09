"""Этап 3: Gemini не открыл видео → звук → faster-whisper → конспект по расшифровке (W1–W6)."""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.genai import errors

from digest import pipeline
from digest.errors import DigestError, GeminiBusy, VideoUnreadable
from digest.fetchers import VideoMeta, VideoOptions, download_audio
from digest.llm import Digested, DigestLLM
from digest.pipeline import make_note
from digest.render import to_markdown
from digest.schema import Digest
from services.transcribe import Transcriber
from tests.test_fetchers import FakeYDL, _factory
from tests.test_schema_llm import MINIMAL, MINIMAL_JSON, _video_llm

VID = "dQw4w9WgXcQ"
CANON = f"https://www.youtube.com/watch?v={VID}"
REEL = "https://www.instagram.com/reel/C1abc/"
DAY = date(2026, 11, 9)
TRANSCRIPT = "[00:00] Привет\n[02:15] Мультикурсор"


# --- LLM: какие ошибки значат «видео не открылось» ------------------------------------------


def _llm_raising(exc):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=exc)
    return DigestLLM(api_key="k", model="m", client=client)


@pytest.mark.parametrize("code", [400, 403, 404])
async def test_w1_youtube_client_error_is_video_unreadable(code):
    exc = errors.ClientError(code, {"error": {"message": "bad video", "status": "X"}})
    with pytest.raises(VideoUnreadable, match="не смог открыть"):
        await _llm_raising(exc).digest_youtube(CANON)


async def test_w3_overload_is_not_video_unreadable():
    exc = errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    with pytest.raises(GeminiBusy):
        await _llm_raising(exc).digest_youtube(CANON)


async def test_w1_text_client_error_is_plain_digest_error():
    exc = errors.ClientError(400, {"error": {"message": "bad", "status": "X"}})
    with pytest.raises(DigestError) as info:
        await _llm_raising(exc).digest_text("текст")
    assert not isinstance(info.value, VideoUnreadable)


async def test_w2_files_api_failed_and_timeout_are_video_unreadable(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    llm, _ = _video_llm(["PROCESSING", "FAILED"])
    with pytest.raises(VideoUnreadable):
        await llm.digest_video_file(video, "video/mp4")
    llm, _ = _video_llm(["PROCESSING"] * 50, timeout_s=0)
    with pytest.raises(VideoUnreadable):
        await llm.digest_video_file(video, "video/mp4")


async def test_w1_bad_json_is_not_video_unreadable():
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text="not json", parsed=None)
    )
    llm = DigestLLM(api_key="k", model="m", client=client)
    with pytest.raises(DigestError) as info:
        await llm.digest_youtube(CANON)
    assert not isinstance(info.value, VideoUnreadable)


async def test_w1_transcript_prompt():
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text=MINIMAL_JSON, parsed=None)
    )
    llm = DigestLLM(api_key="k", model="m", client=client)
    await llm.digest_text(TRANSCRIPT, title="Хоткеи", hint="кратко", transcript=True)
    prompt = client.aio.models.generate_content.call_args.kwargs["contents"].parts[0].text
    assert "расшифровк" in prompt.lower()
    assert "[MM:SS]" in prompt
    assert "кратко" in prompt and "Хоткеи" in prompt and TRANSCRIPT in prompt


# --- Transcriber: сегменты с таймкодами, язык — авто ------------------------------------------


class FakeModel:
    def __init__(self):
        self.kwargs = None

    def transcribe(self, path, **kwargs):
        self.kwargs = kwargs
        segs = [
            SimpleNamespace(start=0.4, text=" Привет"),
            SimpleNamespace(start=135.9, text=" Мультикурсор "),
            SimpleNamespace(start=3725.0, text=" Конец"),
            SimpleNamespace(start=3800.0, text="   "),
        ]
        return iter(segs), None


async def test_transcribe_timed_lines_and_auto_language(tmp_path):
    model = FakeModel()
    t = Transcriber("small", "cpu", "int8", model_factory=lambda *a, **k: model)
    text = await t.transcribe_timed(tmp_path / "a.m4a")
    assert text == "[00:00] Привет\n[02:15] Мультикурсор\n[1:02:05] Конец"
    assert model.kwargs["language"] is None  # видео бывают на английском
    assert model.kwargs["vad_filter"] is True


# --- download_audio ---------------------------------------------------------------------------


async def test_w1_download_audio_bestaudio(tmp_path):
    info = {"id": VID, "ext": "m4a", "title": "Хоткеи", "uploader": "Fireship", "duration": 300}
    path, meta = await download_audio(
        CANON, tmp_path, VideoOptions(), max_seconds=1800, ydl_factory=_factory(info=info)
    )
    assert path == tmp_path / f"{VID}.m4a" and path.is_file()
    assert meta.title == "Хоткеи" and meta.duration == 300
    opts = FakeYDL.instances[0].opts
    assert opts["format"].startswith("ba")


async def test_w4_too_long_for_whisper_not_downloaded(tmp_path):
    info = {"id": VID, "ext": "m4a", "title": "Лекция", "duration": 3000}
    with pytest.raises(DigestError, match="30 мин"):
        await download_audio(
            CANON, tmp_path, VideoOptions(), max_seconds=1800, ydl_factory=_factory(info=info)
        )
    assert FakeYDL.instances[0].calls == ["extract"]


# --- Рендер: via -------------------------------------------------------------------------------


def test_render_via_whisper():
    d = Digest.model_validate(MINIMAL)
    md = to_markdown(d, url=CANON, source="youtube", author="", date_added=DAY, via="whisper")
    assert "\nvia: whisper\n" in md
    plain = to_markdown(d, url=CANON, source="youtube", author="", date_added=DAY)
    assert "via:" not in plain


# --- pipeline -----------------------------------------------------------------------------------


def _http():
    payload = {"title": "Хоткеи VS Code", "author_name": "Fireship"}
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )


def _llm(video_exc=None, text_exc=None, confidence="high"):
    llm = MagicMock()
    data = dict(MINIMAL, confidence=confidence, timestamps=[{"seconds": 135, "label": "мк"}])
    ok = Digested(Digest.model_validate(data), "gemini-test")
    llm.digest_youtube = AsyncMock(return_value=ok, side_effect=video_exc)
    llm.digest_video_file = AsyncMock(return_value=ok, side_effect=video_exc)
    llm.digest_text = AsyncMock(return_value=ok, side_effect=text_exc)
    return llm


def _transcriber(text=TRANSCRIPT):
    t = MagicMock()
    t.transcribe_timed = AsyncMock(return_value=text)
    return t


@pytest.fixture
def fake_audio(monkeypatch):
    seen: dict = {"calls": 0}

    async def download(url, out_dir: Path, opts, *, max_seconds, **kw):
        seen["calls"] += 1
        seen["url"], seen["max_seconds"] = url, max_seconds
        if seen.get("exc"):
            raise seen["exc"]
        path = out_dir / f"{VID}.m4a"
        path.write_bytes(b"audio")
        seen["path"] = path
        return path, VideoMeta(title="Хоткеи VS Code", author="Fireship", duration=300)

    monkeypatch.setattr(pipeline, "download_audio", download)
    return seen


@pytest.fixture
def fake_video(monkeypatch):
    seen: dict = {}

    async def download(url, out_dir: Path, opts, **kw):
        path = out_dir / "C1abc.mp4"
        path.write_bytes(b"video")
        seen["path"] = path
        return path, VideoMeta(
            title="Reel", author="u", duration=seen.get("duration", 42), key="instagram:C1abc"
        )

    monkeypatch.setattr(pipeline, "download_video", download)
    return seen


UNREADABLE = VideoUnreadable("Gemini не смог открыть видео: bad")


async def test_w1_youtube_falls_back_to_whisper(fake_audio):
    llm, tr = _llm(video_exc=UNREADABLE), _transcriber()
    progress = AsyncMock()
    async with _http() as http:
        note = await make_note(
            CANON, "упор", llm=llm, http=http, today=DAY, transcriber=tr, progress=progress
        )
    assert fake_audio["url"] == CANON and fake_audio["max_seconds"] == 1800
    tr.transcribe_timed.assert_awaited_once_with(fake_audio["path"])
    llm.digest_text.assert_awaited_once_with(
        TRANSCRIPT, title="Хоткеи VS Code", hint="упор", transcript=True
    )
    assert "\nvia: whisper\n" in note.markdown
    assert "confidence: medium" in note.markdown  # high понижен: экрана модель не видела
    assert f"https://youtu.be/{VID}?t=135" in note.markdown
    assert note.url == CANON
    texts = [c.args[0] for c in progress.await_args_list]
    assert any(t.startswith("🎧") for t in texts)


async def test_w1_low_confidence_kept(fake_audio):
    llm = _llm(video_exc=UNREADABLE, confidence="low")
    async with _http() as http:
        note = await make_note(CANON, "", llm=llm, http=http, today=DAY, transcriber=_transcriber())
    assert "confidence: low" in note.markdown


async def test_w2_reel_whisper_uses_downloaded_file(fake_video, fake_audio):
    llm, tr = _llm(video_exc=UNREADABLE), _transcriber()
    async with _http() as http:
        note = await make_note(REEL, "", llm=llm, http=http, today=DAY, transcriber=tr)
    tr.transcribe_timed.assert_awaited_once_with(fake_video["path"])
    assert fake_audio["calls"] == 0  # второй раз не качаем
    assert "\nvia: whisper\n" in note.markdown


async def test_w4_reel_too_long_for_whisper(fake_video):
    fake_video["duration"] = 2000
    llm, tr = _llm(video_exc=UNREADABLE), _transcriber()
    async with _http() as http:
        with pytest.raises(DigestError, match="30 мин"):
            await make_note(REEL, "", llm=llm, http=http, today=DAY, transcriber=tr)
    tr.transcribe_timed.assert_not_awaited()


async def test_w3_busy_does_not_use_whisper(fake_audio):
    llm, tr = _llm(video_exc=GeminiBusy("перегружен")), _transcriber()
    async with _http() as http:
        with pytest.raises(GeminiBusy):
            await make_note(CANON, "", llm=llm, http=http, today=DAY, transcriber=tr)
    assert fake_audio["calls"] == 0
    tr.transcribe_timed.assert_not_awaited()


async def test_w3_busy_on_transcript_digest_propagates(fake_audio):
    """Расшифровали, но Gemini перегружен на тексте — задача откладывается, как обычно."""
    llm = _llm(video_exc=UNREADABLE, text_exc=GeminiBusy("перегружен"))
    async with _http() as http:
        with pytest.raises(GeminiBusy):
            await make_note(CANON, "", llm=llm, http=http, today=DAY, transcriber=_transcriber())


async def test_w5_empty_transcript(fake_audio):
    llm = _llm(video_exc=UNREADABLE)
    async with _http() as http:
        with pytest.raises(DigestError, match="нет речи"):
            await make_note(
                CANON, "", llm=llm, http=http, today=DAY, transcriber=_transcriber("  ")
            )
    llm.digest_text.assert_not_awaited()


async def test_w5_audio_download_failed(fake_audio):
    fake_audio["exc"] = DigestError("Не смог скачать видео: Sign in to confirm")
    llm = _llm(video_exc=UNREADABLE)
    async with _http() as http:
        with pytest.raises(DigestError, match="Gemini не открыл видео.*звук.*Sign in"):
            await make_note(CANON, "", llm=llm, http=http, today=DAY, transcriber=_transcriber())


async def test_w6_no_transcriber_keeps_old_error(fake_audio):
    llm = _llm(video_exc=UNREADABLE)
    async with _http() as http:
        with pytest.raises(VideoUnreadable):
            await make_note(CANON, "", llm=llm, http=http, today=DAY)
    assert fake_audio["calls"] == 0


async def test_w6_normal_note_has_url_and_no_via():
    async with _http() as http:
        note = await make_note(CANON, "", llm=_llm(), http=http, today=DAY)
    assert note.url == CANON
    assert "via:" not in note.markdown


def test_minimal_json_is_valid():
    assert json.loads(MINIMAL_JSON)["title"]
