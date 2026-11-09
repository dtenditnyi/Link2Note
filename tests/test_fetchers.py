import httpx
import pytest

from digest.errors import DigestError
from digest.fetchers import VideoMeta, youtube_meta

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_f1_ok():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = request.url
        return httpx.Response(200, json={"title": "Видео", "author_name": "Fireship"})

    async with _client(handler) as c:
        meta = await youtube_meta(URL, c)
    assert meta == VideoMeta(title="Видео", author="Fireship")
    assert seen["url"].host == "www.youtube.com"
    assert seen["url"].path == "/oembed"
    assert seen["url"].params["url"] == URL
    assert seen["url"].params["format"] == "json"


@pytest.mark.parametrize("status", [401, 403])
async def test_f1_private(status):
    async with _client(lambda r: httpx.Response(status)) as c:
        with pytest.raises(DigestError, match="приватн"):
            await youtube_meta(URL, c)


async def test_f1_not_found():
    async with _client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(DigestError, match="не найдено"):
            await youtube_meta(URL, c)


async def test_f1_network_error_gives_empty_meta():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as c:
        assert await youtube_meta(URL, c) == VideoMeta()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_f1_other_status_gives_empty_meta(status):
    async with _client(lambda r: httpx.Response(status)) as c:
        assert await youtube_meta(URL, c) == VideoMeta()


async def test_f1_bad_json_gives_empty_meta():
    async with _client(lambda r: httpx.Response(200, text="<html>")) as c:
        assert await youtube_meta(URL, c) == VideoMeta()
