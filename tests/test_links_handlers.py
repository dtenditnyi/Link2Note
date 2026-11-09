import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import MessageEntity

from digest.worker import Job
from handlers.links import MAX_LINKS, has_links, on_links
from tests.conftest import OWNER, make_message, sent_texts

VID = "dQw4w9WgXcQ"
YT = f"https://youtu.be/{VID}"
REEL = "https://www.instagram.com/reel/C1abc/"
ARTICLE = "https://habr.com/ru/articles/1/"


def _msg(text=None, *, caption=None, entities=None, caption_entities=None):
    msg = make_message(text)
    msg.caption = caption
    msg.entities = entities
    msg.caption_entities = caption_entities
    msg.forward_origin = None  # как у настоящего непересланного Message
    ids = iter(range(100, 200))
    msg.reply = AsyncMock(side_effect=lambda *a, **k: MagicMock(message_id=next(ids)))
    return msg


def _jobs(queue: asyncio.Queue) -> list[Job]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def _handle(msg, urls, db, *, vault_ready=True) -> list[Job]:
    queue: asyncio.Queue[Job] = asyncio.Queue()
    await on_links(msg, urls=urls, link_queue=queue, vault_ready=vault_ready, db=db)
    return _jobs(queue)


async def test_filter_no_links():
    assert await has_links(_msg("просто текст")) is False
    assert await has_links(_msg(None)) is False


async def test_filter_returns_urls_from_caption_entities():
    caption = "пост"
    ents = [MessageEntity(type="text_link", offset=0, length=4, url=YT)]
    assert await has_links(_msg(None, caption=caption, caption_entities=ents)) == {"urls": [YT]}


async def test_t1_link_with_hint_queued(db):
    msg = _msg(f"{YT} сделай упор на хоткеи")
    jobs = await _handle(msg, [YT], db)
    assert sent_texts(msg.reply)[0].startswith("⏳")
    [pending] = db.pending_jobs()
    assert jobs == [
        Job(
            url=YT,
            hint="сделай упор на хоткеи",
            chat_id=OWNER,
            status_msg_id=100,
            force=False,
            id=pending.id,
        )
    ]
    assert (pending.url, pending.hint, pending.status_msg_id) == (YT, "сделай упор на хоткеи", 100)


async def test_t1_hint_empty_when_only_link(db):
    assert (await _handle(_msg(f"  {YT}  "), [YT], db))[0].hint == ""


async def test_t2_caption_link_queued(db):
    jobs = await _handle(_msg(None, caption=f"Классное видео {YT}"), [YT], db)
    assert [j.url for j in jobs] == [YT]
    assert jobs[0].hint == "Классное видео"


async def test_t3_all_sources_queued(db):
    urls = [
        ARTICLE,
        REEL,
        "https://www.tiktok.com/@u/video/7301234567890123456",
        "https://x.com/u/status/1",
    ]
    msg = _msg(" ".join(urls))
    jobs = await _handle(msg, urls, db)
    assert [j.url for j in jobs] == urls
    assert len(db.pending_jobs()) == 4
    assert all(t.startswith("⏳") for t in sent_texts(msg.reply))


async def test_t3_youtube_channel_rejected(db):
    for url in ("https://www.youtube.com/@channel", "https://www.youtube.com/playlist?list=PL1"):
        msg = _msg(url)
        assert await _handle(msg, [url], db) == []
        assert "ролик" in sent_texts(msg.reply)[0]
    assert db.pending_jobs() == []


async def test_t3_mixed_links_reports_skipped(db):
    channel = "https://www.youtube.com/@channel"
    msg = _msg(f"{YT} {channel} {ARTICLE}")
    jobs = await _handle(msg, [YT, channel, ARTICLE], db)
    assert [j.url for j in jobs] == [YT, ARTICLE]
    assert any("пропустил" in t for t in sent_texts(msg.reply))


async def test_t7_vault_not_configured(db):
    msg = _msg(YT)
    assert await _handle(msg, [YT], db, vault_ready=False) == []
    assert "VAULT_REPO" in sent_texts(msg.reply)[0]
    assert db.pending_jobs() == []


async def test_t8_max_links(db):
    urls = [f"https://youtu.be/{VID[:-1]}{c}" for c in "ABCDEFG"]
    msg = _msg(" ".join(urls))
    jobs = await _handle(msg, urls, db)
    assert [j.url for j in jobs] == urls[:MAX_LINKS]
    assert MAX_LINKS == 5
    assert any("первые 5" in t for t in sent_texts(msg.reply))


async def test_t2_text_link_through_handler(db):
    """Ссылка «под словом»: в видимом тексте URL нет, только text_link-entity."""
    ents = [MessageEntity(type="text_link", offset=6, length=3, url=YT)]
    msg = _msg("глянь это видео", entities=ents)
    found = await has_links(msg)
    assert isinstance(found, dict) and found == {"urls": [YT]}
    assert [j.url for j in await _handle(msg, found["urls"], db)] == [YT]


async def test_forwarded_post_text_is_not_hint(db):
    msg = _msg(None, caption=f"Подписывайтесь на канал, лучшие видео! Заново! {YT}")
    msg.forward_origin = MagicMock()
    [job] = await _handle(msg, [YT], db)
    assert job.hint == ""
    assert job.force is False  # «заново» в чужом посте — не моя команда


async def test_hint_capped(db):
    jobs = await _handle(_msg(f"{YT} " + "очень длинно " * 100), [YT], db)
    assert len(jobs[0].hint) <= 300


async def test_same_video_different_forms_one_job(db):
    a, b = YT, f"https://www.youtube.com/watch?v={VID}&si=x"
    assert [j.url for j in await _handle(_msg(f"{a} {b}"), [a, b], db)] == [a]


async def test_t9_same_reel_different_forms_one_job(db):
    a, b = REEL, "https://www.instagram.com/reel/C1abc/?igsh=MWx0"
    assert [j.url for j in await _handle(_msg(f"{a} {b}"), [a, b], db)] == [a]


async def test_t10_force_word(db):
    [job] = await _handle(_msg(f"{REEL} заново"), [REEL], db)
    assert job.force is True
    assert job.hint == ""
    assert db.pending_jobs()[0].force is True


async def test_t10_force_word_with_hint(db):
    [job] = await _handle(_msg(f"{REEL} Заново, упор на хоткеи"), [REEL], db)
    assert (job.force, job.hint) == (True, "упор на хоткеи")


def _forwarded(text):
    msg = _msg(None, caption=text)
    msg.forward_origin = MagicMock()
    return msg


async def test_t11_forwarded_post_takes_only_videos(db):
    shop, tg = "https://shop.example.com/promo", "https://t.me/channel/123"
    urls = [shop, YT, tg, REEL]
    jobs = await _handle(_forwarded(" ".join(urls)), urls, db)
    assert [j.url for j in jobs] == [YT, REEL]


async def test_t11_forwarded_post_without_video_takes_first_article(db):
    tg, other = "https://t.me/channel/123", "https://example.com/other"
    urls = [tg, ARTICLE, other]
    jobs = await _handle(_forwarded(" ".join(urls)), urls, db)
    assert [j.url for j in jobs] == [ARTICLE]


async def test_t11_own_message_keeps_all_links(db):
    urls = [ARTICLE, "https://example.com/other"]
    assert [j.url for j in await _handle(_msg(" ".join(urls)), urls, db)] == urls


async def test_t11_forwarded_post_only_telegram_links(db):
    msg = _forwarded("https://t.me/channel/1 https://t.me/other")
    assert await _handle(msg, ["https://t.me/channel/1", "https://t.me/other"], db) == []
    assert "нет ссылок" in sent_texts(msg.reply)[0]
