import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import MessageEntity

from digest.worker import Job
from handlers.links import MAX_LINKS, has_links, on_links
from tests.conftest import OWNER, make_message, sent_texts

VID = "dQw4w9WgXcQ"
YT = f"https://youtu.be/{VID}"


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


async def test_filter_no_links():
    assert await has_links(_msg("просто текст")) is False
    assert await has_links(_msg(None)) is False


async def test_filter_returns_urls_from_caption_entities():
    caption = "пост"
    ents = [MessageEntity(type="text_link", offset=0, length=4, url=YT)]
    assert await has_links(_msg(None, caption=caption, caption_entities=ents)) == {"urls": [YT]}


async def test_t1_link_with_hint_queued():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    msg = _msg(f"{YT} сделай упор на хоткеи")
    await on_links(msg, urls=[YT], link_queue=queue, vault_ready=True)
    assert sent_texts(msg.reply)[0].startswith("⏳")
    assert _jobs(queue) == [
        Job(url=YT, hint="сделай упор на хоткеи", chat_id=OWNER, status_msg_id=100)
    ]


async def test_t1_hint_empty_when_only_link():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    await on_links(_msg(f"  {YT}  "), urls=[YT], link_queue=queue, vault_ready=True)
    assert _jobs(queue)[0].hint == ""


async def test_t2_caption_link_queued():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    msg = _msg(None, caption=f"Классное видео {YT}")
    await on_links(msg, urls=[YT], link_queue=queue, vault_ready=True)
    jobs = _jobs(queue)
    assert [j.url for j in jobs] == [YT]
    assert jobs[0].hint == "Классное видео"


async def test_t3_non_youtube_not_queued():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    for url in (
        "https://habr.com/ru/articles/1/",
        "https://www.instagram.com/reel/abc/",
        "https://www.youtube.com/@channel",
    ):
        msg = _msg(url)
        await on_links(msg, urls=[url], link_queue=queue, vault_ready=True)
        assert "только" in sent_texts(msg.reply)[0] and "YouTube" in sent_texts(msg.reply)[0]
    assert queue.empty()


async def test_t3_mixed_links_reports_skipped():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    other = "https://habr.com/ru/articles/1/"
    msg = _msg(f"{YT} {other}")
    await on_links(msg, urls=[YT, other], link_queue=queue, vault_ready=True)
    assert [j.url for j in _jobs(queue)] == [YT]
    assert any("пропустил" in t for t in sent_texts(msg.reply))


async def test_t7_vault_not_configured():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    msg = _msg(YT)
    await on_links(msg, urls=[YT], link_queue=queue, vault_ready=False)
    assert queue.empty()
    assert "VAULT_REPO" in sent_texts(msg.reply)[0]


async def test_t8_max_links():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    urls = [f"https://youtu.be/{VID[:-1]}{c}" for c in "ABCDEFG"]
    msg = _msg(" ".join(urls))
    await on_links(msg, urls=urls, link_queue=queue, vault_ready=True)
    assert [j.url for j in _jobs(queue)] == urls[:MAX_LINKS]
    assert MAX_LINKS == 5
    assert any("первые 5" in t for t in sent_texts(msg.reply))


# --- После ревью ------------------------------------------------------------------------


async def test_t2_text_link_through_handler():
    """Ссылка «под словом»: в видимом тексте URL нет, только text_link-entity."""
    queue: asyncio.Queue[Job] = asyncio.Queue()
    ents = [MessageEntity(type="text_link", offset=6, length=3, url=YT)]
    msg = _msg("глянь это видео", entities=ents)
    found = await has_links(msg)
    assert isinstance(found, dict) and found == {"urls": [YT]}
    await on_links(msg, urls=found["urls"], link_queue=queue, vault_ready=True)
    assert [j.url for j in _jobs(queue)] == [YT]


async def test_forwarded_post_text_is_not_hint():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    msg = _msg(None, caption=f"Подписывайтесь на канал, лучшие видео! {YT}")
    msg.forward_origin = MagicMock()
    await on_links(msg, urls=[YT], link_queue=queue, vault_ready=True)
    assert _jobs(queue)[0].hint == ""


async def test_hint_capped():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    msg = _msg(f"{YT} " + "очень длинно " * 100)
    await on_links(msg, urls=[YT], link_queue=queue, vault_ready=True)
    assert len(_jobs(queue)[0].hint) <= 300


async def test_same_video_different_forms_one_job():
    queue: asyncio.Queue[Job] = asyncio.Queue()
    a, b = YT, f"https://www.youtube.com/watch?v={VID}&si=x"
    msg = _msg(f"{a} {b}")
    await on_links(msg, urls=[a, b], link_queue=queue, vault_ready=True)
    assert [j.url for j in _jobs(queue)] == [a]
