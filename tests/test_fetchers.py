import asyncio
from pathlib import Path

import httpx
import pytest
from yt_dlp.utils import DownloadError

from digest.errors import DigestError
from digest.fetchers import (
    Article,
    VideoMeta,
    VideoOptions,
    download_video,
    fetch_article,
    youtube_meta,
)

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


# --- Этап 2: yt-dlp (YoutubeDL подменён) -------------------------------------------------

REEL = "https://www.instagram.com/reel/C1abc/"
INFO = {
    "id": "C1abc",
    "extractor_key": "Instagram",
    "title": "Reel title",
    "uploader": "some.user",
    "duration": 42.6,
    "ext": "mp4",
}


class FakeYDL:
    """Минимальный YoutubeDL: запоминает опции и вызовы, «скачивает» пустой файл."""

    instances: list["FakeYDL"] = []

    def __init__(self, opts, *, info=None, error=None, overwrite_cookies=False):
        self.opts = opts
        self.info = INFO if info is None else info
        self.error = error
        self.overwrite_cookies = overwrite_cookies
        self.calls: list[bool] = []
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Как настоящий yt-dlp: при закрытии пишет cookies обратно в cookiefile.
        if self.overwrite_cookies and self.opts.get("cookiefile"):
            Path(self.opts["cookiefile"]).write_text("# rewritten by yt-dlp\n")

    def extract_info(self, url, download=False):
        self.calls.append("extract+download" if download else "extract")
        if self.error:
            raise self.error
        return self.info

    def process_ie_result(self, info, download=True):
        """Как yt-dlp: скачивает уже извлечённое и пишет путь в requested_downloads."""
        self.calls.append(f"process:{info['id']}")
        path = Path(self.prepare_filename(info))
        path.write_bytes(b"video")
        (path.parent / f"{info['id']}.f137.mp4.part").write_bytes(b"leftover")
        return {**info, "requested_downloads": [{"filepath": str(path)}]}

    def prepare_filename(self, info):
        return self.opts["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", info["ext"])


def _factory(**kw):
    FakeYDL.instances = []
    return lambda opts: FakeYDL(opts, **kw)


async def test_y4_download_ok(tmp_path):
    path, meta = await download_video(REEL, tmp_path, VideoOptions(), ydl_factory=_factory())
    assert path == tmp_path / "C1abc.mp4" and path.exists()
    assert meta == VideoMeta(
        title="Reel title", author="some.user", duration=43, key="instagram:C1abc"
    )
    ydl = FakeYDL.instances[0]
    # Одно извлечение (меньше запросов к Instagram), потом скачивание уже извлечённого.
    assert ydl.calls == ["extract", "process:C1abc"]
    assert ydl.opts["format"].startswith("b[height<=720]")
    assert ydl.opts["noplaylist"] is True
    assert ydl.opts["max_filesize"] == 300 * 1024 * 1024
    assert "cookiefile" not in ydl.opts


async def test_y5_carousel_takes_first_video(tmp_path):
    first = {**INFO, "id": "V1", "duration": 10}
    second = {**INFO, "id": "V2", "duration": 20}
    post = {
        "_type": "playlist",
        "id": "POST",
        "extractor_key": "Instagram",
        "entries": [first, second],
    }
    path, meta = await download_video(
        REEL, tmp_path, VideoOptions(), ydl_factory=_factory(info=post)
    )
    assert path == tmp_path / "V1.mp4"
    assert FakeYDL.instances[0].calls == ["extract", "process:V1"]
    assert meta.duration == 10
    assert meta.key == "instagram:V1"


async def test_y5_empty_playlist_no_video(tmp_path):
    post = {"_type": "playlist", "id": "POST", "entries": []}
    with pytest.raises(DigestError, match="нет видео"):
        await download_video(REEL, tmp_path, VideoOptions(), ydl_factory=_factory(info=post))


async def test_y3_login_message_not_instagram_specific_for_other_sites(tmp_path):
    err = DownloadError("ERROR: [twitter] 1: Requested tweet is unavailable. Use --cookies")
    with pytest.raises(DigestError) as exc:
        await download_video(
            "https://x.com/u/status/1", tmp_path, VideoOptions(), ydl_factory=_factory(error=err)
        )
    assert "COOKIES_FILE" in str(exc.value)
    assert "Instagram" not in str(exc.value)


async def test_y1_too_long_rejected_before_download(tmp_path):
    info = {**INFO, "duration": 3601}
    with pytest.raises(DigestError, match="длиннее"):
        await download_video(
            REEL, tmp_path, VideoOptions(max_seconds=3600), ydl_factory=_factory(info=info)
        )
    assert FakeYDL.instances[0].calls == ["extract"]


async def test_y2_cookies_copied_original_untouched(tmp_path):
    original = tmp_path / "secrets" / "cookies.txt"
    original.parent.mkdir()
    original.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsid\tx\n")
    before = original.read_text()
    out = tmp_path / "job"
    out.mkdir()
    await download_video(
        REEL,
        out,
        VideoOptions(cookies_file=original),
        ydl_factory=_factory(overwrite_cookies=True),
    )
    used = Path(FakeYDL.instances[0].opts["cookiefile"])
    assert used != original
    assert used.parent == out
    assert original.read_text() == before


async def test_y2_missing_cookies_file_is_clear_error(tmp_path):
    with pytest.raises(DigestError, match="COOKIES_FILE"):
        await download_video(
            REEL,
            tmp_path,
            VideoOptions(cookies_file=tmp_path / "nope.txt"),
            ydl_factory=_factory(),
        )


LOGIN = DownloadError(
    "ERROR: [Instagram] C1abc: Requested content is not available, rate-limit reached "
    "or login required. Use --cookies, --cookies-from-browser, --username and --password"
)


async def test_y3_login_required_without_cookies(tmp_path):
    with pytest.raises(DigestError, match="COOKIES_FILE"):
        await download_video(REEL, tmp_path, VideoOptions(), ydl_factory=_factory(error=LOGIN))


async def test_y3_login_required_with_cookies_says_stale(tmp_path):
    cookies = tmp_path / "c.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    out = tmp_path / "job"
    out.mkdir()
    with pytest.raises(DigestError, match="устарели"):
        await download_video(
            REEL, out, VideoOptions(cookies_file=cookies), ydl_factory=_factory(error=LOGIN)
        )


async def test_y3_no_video(tmp_path):
    err = DownloadError("ERROR: [twitter] 123: No video could be found in this tweet")
    with pytest.raises(DigestError, match="нет видео"):
        await download_video(
            "https://x.com/u/status/123", tmp_path, VideoOptions(), ydl_factory=_factory(error=err)
        )


async def test_y3_other_error_last_line(tmp_path):
    err = DownloadError("WARNING: something\nERROR: [TikTok] 1: HTTP Error 500")
    with pytest.raises(DigestError, match="(?i)не смог скачать видео.*HTTP Error 500"):
        await download_video(REEL, tmp_path, VideoOptions(), ydl_factory=_factory(error=err))


# --- Этап 2: статьи (httpx замокан, trafilatura настоящий) ------------------------------

ARTICLE_URL = "https://blog.example.com/posts/vs-code-tips"
# Абзацы разные: trafilatura схлопывает одинаковые, как в настоящих статьях их и не бывает.
PARAS = [
    "Мультикурсор в VS Code позволяет редактировать сразу несколько мест в файле.",
    "Нажмите Ctrl+Shift+L, чтобы поставить курсор на все совпадения выделенного слова.",
    "Сочетание Alt+Up переносит текущую строку вверх без копирования и вставки.",
    "Палитра команд по Ctrl+Shift+P открывает любую функцию редактора по названию.",
    "Быстрый переход к символу в файле — Ctrl+Shift+O, к файлу в проекте — Ctrl+P.",
]
HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Советы по VS Code | Example Blog</title>
<meta name="author" content="Иван Петров">
<meta property="og:site_name" content="Example Blog">
</head><body><nav>Меню Главная О нас</nav>
<article><h1>Советы по VS Code</h1>
{"".join(f"<p>{p}</p>" for p in PARAS)}
</article><footer>© 2026</footer></body></html>"""


def _page(
    status=200, body: str | bytes = HTML, content_type="text/html; charset=utf-8", headers=None
):
    h = {"content-type": content_type, **(headers or {})}
    content = body.encode() if isinstance(body, str) else body
    return _client(lambda r: httpx.Response(status, content=content, headers=h))


async def test_a1_article_ok():
    async with _page() as c:
        art = await fetch_article(ARTICLE_URL, c)
    assert isinstance(art, Article)
    assert "Мультикурсор" in art.text
    assert "Меню Главная" not in art.text
    assert "VS Code" in art.title
    assert art.author == "Иван Петров"
    assert art.sitename == "Example Blog"


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_a2_closed(status):
    async with _page(status=status) as c:
        with pytest.raises(DigestError, match="закрыт"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_not_found():
    async with _page(status=404) as c:
        with pytest.raises(DigestError, match="не найдена"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_server_error():
    async with _page(status=500) as c:
        with pytest.raises(DigestError, match="500"):
            await fetch_article(ARTICLE_URL, c)


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "video/mp4"])
async def test_a2_not_html(ctype):
    async with _page(body=b"%PDF-1.7", content_type=ctype) as c:
        with pytest.raises(DigestError, match="веб-страниц"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_too_big():
    big = b"<html><body>" + b"x" * (6 * 1024 * 1024) + b"</body></html>"
    async with _page(body=big) as c:
        with pytest.raises(DigestError, match="больш"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_too_big_by_header():
    async with _page(headers={"content-length": str(50 * 1024 * 1024)}) as c:
        with pytest.raises(DigestError, match="больш"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_overall_timeout(monkeypatch):
    """Сервер, отдающий страницу по капле, не держит очередь дольше общего таймаута."""
    import digest.fetchers as f

    monkeypatch.setattr(f, "ARTICLE_TIMEOUT_S", 0.2)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b"<p>x</p>"

    async def handler(request):
        return httpx.Response(200, stream=Slow(), headers={"content-type": "text/html"})

    async with _client(handler) as c:
        with pytest.raises(DigestError, match="(?i)долго"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_too_little_text():
    html = "<html><body><div id='app'>Loading…</div><script>app()</script></body></html>"
    async with _page(body=html) as c:
        with pytest.raises(DigestError, match="(?i)не смог вытащить текст"):
            await fetch_article(ARTICLE_URL, c)


async def test_a2_network_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as c:
        with pytest.raises(DigestError, match="(?i)не смог открыть"):
            await fetch_article(ARTICLE_URL, c)


async def test_a_user_agent_is_browser_like():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, content=HTML.encode(), headers={"content-type": "text/html"})

    async with _client(handler) as c:
        await fetch_article(ARTICLE_URL, c)
    assert seen["ua"].startswith("Mozilla/5.0")
    assert "@" not in seen["ua"]
