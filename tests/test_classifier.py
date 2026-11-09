import pytest
from aiogram.types import MessageEntity

from digest.classifier import (
    Source,
    canonical_youtube,
    classify,
    extract_urls,
    normalize,
    split_force,
    youtube_id,
)

VID = "dQw4w9WgXcQ"

YOUTUBE_FORMS = [
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=abcdef",
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&si=x&t=5",
    f"https://www.youtube.com/watch?list=PL123&v={VID}&index=2",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://youtube.com/shorts/{VID}?feature=share",
    f"https://www.youtube.com/live/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube-nocookie.com/embed/{VID}",
    f"https://music.youtube.com/watch?v={VID}",
    f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}",
]


@pytest.mark.parametrize("url", YOUTUBE_FORMS)
def test_c1_youtube_forms(url):
    assert classify(url) is Source.YOUTUBE
    assert youtube_id(url) == VID
    assert normalize(url) == f"youtube:{VID}"
    assert canonical_youtube(VID) == f"https://www.youtube.com/watch?v={VID}"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.instagram.com/reel/C1abcDEF/", Source.INSTAGRAM),
        ("https://instagram.com/p/C1abcDEF/", Source.INSTAGRAM),
        ("https://www.tiktok.com/@user/video/7300000000000000000", Source.TIKTOK),
        ("https://vm.tiktok.com/ZMabc/", Source.TIKTOK),
        ("https://x.com/user/status/1", Source.VIDEO),
        ("https://twitter.com/user/status/1", Source.VIDEO),
        ("https://vimeo.com/123456", Source.VIDEO),
        ("https://habr.com/ru/articles/123/", Source.ARTICLE),
        ("https://docs.python.org/3/library/asyncio.html", Source.ARTICLE),
        # Похоже на YouTube, но не он
        ("https://notyoutube.com/watch?v=dQw4w9WgXcQ", Source.ARTICLE),
        ("https://youtube.com.evil.io/watch?v=dQw4w9WgXcQ", Source.ARTICLE),
        ("https://evilinstagram.com/reel/1", Source.ARTICLE),
    ],
)
def test_c2_other_sources(url, expected):
    assert classify(url) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@fireship",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/",
        "https://youtu.be/",
        "https://youtu.be/abc",  # не 11 символов — не ID видео
        "https://www.youtube.com/watch?v=bad<id>",
        "https://habr.com/ru/articles/123/",
    ],
)
def test_c3_no_video_id(url):
    assert youtube_id(url) is None


def test_normalize_other_strips_query_and_slash():
    assert normalize("https://www.habr.com/ru/articles/123/?utm_source=tg") == (
        "habr.com/ru/articles/123"
    )


def test_c4_trailing_punctuation_trimmed():
    assert extract_urls(f"смотри https://youtu.be/{VID}).", None) == [f"https://youtu.be/{VID}"]
    assert extract_urls(f"(https://youtu.be/{VID}), и всё!", None) == [f"https://youtu.be/{VID}"]
    assert extract_urls(f"«https://youtu.be/{VID}»", None) == [f"https://youtu.be/{VID}"]


def test_c4_parentheses_inside_url_kept():
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert extract_urls(f"см. {url}", None) == [url]


def test_c4_text_link_entity():
    text = "смотри это видео"
    entities = [
        MessageEntity(type="text_link", offset=6, length=3, url=f"https://youtu.be/{VID}"),
    ]
    assert extract_urls(text, entities) == [f"https://youtu.be/{VID}"]


def test_c4_url_entity_with_emoji_offsets():
    """Смещения entities — в UTF-16; эмодзи перед ссылкой их сдвигает."""
    url = f"https://youtu.be/{VID}"
    text = f"🔥🔥 {url}"
    entities = [MessageEntity(type="url", offset=5, length=len(url))]
    assert extract_urls(text, entities) == [url]


def test_c4_url_without_scheme_from_entity():
    text = "глянь youtu.be/" + VID
    entities = [MessageEntity(type="url", offset=6, length=len("youtu.be/" + VID))]
    assert extract_urls(text, entities) == [f"https://youtu.be/{VID}"]


def test_c4_dedup_and_order():
    a = f"https://youtu.be/{VID}"
    b = "https://habr.com/ru/articles/1/"
    text = f"{a} потом {b} и снова {a}"
    entities = [
        MessageEntity(type="url", offset=0, length=len(a)),
    ]
    assert extract_urls(text, entities) == [a, b]


def test_c4_empty():
    assert extract_urls(None, None) == []
    assert extract_urls("просто текст", []) == []


# --- Этап 2: нормализация для дубликатов и «заново» -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/reel/C1abc-_9/?igsh=MWx0",
        "https://instagram.com/reels/C1abc-_9/",
        "https://www.instagram.com/p/C1abc-_9",
        "https://www.instagram.com/some.user/reel/C1abc-_9/?utm_source=ig_web",
        "https://www.instagram.com/tv/C1abc-_9/",
    ],
)
def test_n1_instagram_forms(url):
    assert normalize(url) == "instagram:C1abc-_9"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@user.name/video/7301234567890123456?is_from_webapp=1&sender=x",
        "https://m.tiktok.com/@u/video/7301234567890123456",
    ],
)
def test_n1_tiktok_forms(url):
    assert normalize(url) == "tiktok:7301234567890123456"


def test_n1_tiktok_short_link_kept_as_url():
    """Короткую ссылку без ID не угадать — ключ по адресу; второй ключ даст yt-dlp."""
    assert normalize("https://vm.tiktok.com/ZMabc123/") == "vm.tiktok.com/ZMabc123"


def test_n2_tracking_params_dropped_and_sorted():
    a = normalize("https://example.com/a?utm_source=x&b=2&a=1#top")
    b = normalize("https://www.example.com/a/?a=1&b=2")
    assert a == b == "example.com/a?a=1&b=2"


def test_n2_meaningful_query_kept():
    one = normalize("https://news.ycombinator.com/item?id=1")
    two = normalize("https://news.ycombinator.com/item?id=2")
    assert one != two
    assert one == "news.ycombinator.com/item?id=1"


@pytest.mark.parametrize(
    "param", ["utm_medium=x", "fbclid=1", "gclid=2", "igsh=3", "igshid=4", "si=5", "ref=tg"]
)
def test_n2_each_tracking_param(param):
    assert normalize(f"https://example.com/post?{param}") == "example.com/post"


def test_n3_youtube_keys_unchanged():
    assert normalize(f"https://youtu.be/{VID}?si=1") == f"youtube:{VID}"


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("заново", ("", True)),
        ("Заново, упор на хоткеи", ("упор на хоткеи", True)),
        ("сделай ЗАНОВО подробно", ("сделай подробно", True)),
        ("упор на хоткеи", ("упор на хоткеи", False)),
        ("зановой", ("зановой", False)),
        ("позаново", ("позаново", False)),
        ("", ("", False)),
    ],
)
def test_n4_force_word(hint, expected):
    assert split_force(hint) == expected
