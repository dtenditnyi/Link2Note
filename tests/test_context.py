from datetime import timedelta

from handlers.context import on_text
from services.context import ContextMemory, contains_url
from tests.conftest import OWNER, make_message


def test_memory_add_and_pop(now):
    m = ContextMemory(ttl=timedelta(minutes=15))
    m.add(OWNER, "рецепт пирога", now)
    m.add(OWNER, "на 6 человек", now + timedelta(minutes=5))
    assert m.pop(OWNER, now + timedelta(minutes=6)) == "рецепт пирога\nна 6 человек"
    assert m.pop(OWNER, now + timedelta(minutes=6)) is None  # V6: очищено после использования


def test_memory_expires(now):
    """A4: через 15 минут память истекает."""
    m = ContextMemory(ttl=timedelta(minutes=15))
    m.add(OWNER, "старое", now)
    assert m.pop(OWNER, now + timedelta(minutes=15, seconds=1)) is None


def test_memory_append_extends_ttl(now):
    m = ContextMemory(ttl=timedelta(minutes=15))
    m.add(OWNER, "a", now)
    m.add(OWNER, "b", now + timedelta(minutes=10))
    assert m.pop(OWNER, now + timedelta(minutes=20)) == "a\nb"


def test_expired_then_new_starts_fresh(now):
    m = ContextMemory(ttl=timedelta(minutes=15))
    m.add(OWNER, "old", now)
    m.add(OWNER, "new", now + timedelta(minutes=30))
    assert m.pop(OWNER, now + timedelta(minutes=31)) == "new"


def test_contains_url():
    assert contains_url("глянь https://youtu.be/abc")
    assert contains_url("http://example.com")
    assert not contains_url("просто текст про youtube")


async def test_plain_text_saved_silently(now):
    """A4: без ответного сообщения."""
    m = ContextMemory(ttl=timedelta(minutes=15))
    msg = make_message("купить муку")
    await on_text(msg, memory=m, clock=lambda: now)
    msg.answer.assert_not_called()
    msg.reply.assert_not_called()
    assert m.pop(OWNER, now) == "купить муку"


async def test_text_with_url_not_stored(now):
    """A3: ссылки остаются для Link2Note."""
    m = ContextMemory(ttl=timedelta(minutes=15))
    msg = make_message("https://youtu.be/abc")
    await on_text(msg, memory=m, clock=lambda: now)
    msg.answer.assert_not_called()
    assert m.pop(OWNER, now) is None


async def test_text_link_entity_not_stored(now):
    """A3: гиперссылка в тексте или ссылка без схемы (Telegram размечает их entity)."""
    from types import SimpleNamespace

    m = ContextMemory(ttl=timedelta(minutes=15))
    for kind in ("text_link", "url"):
        msg = make_message("глянь вот это видео youtu.be/abc")
        msg.entities = [SimpleNamespace(type=kind)]
        await on_text(msg, memory=m, clock=lambda: now)
    assert m.pop(OWNER, now) is None


def test_get_does_not_consume(now):
    m = ContextMemory(ttl=timedelta(minutes=15))
    m.add(OWNER, "x", now)
    assert m.get(OWNER, now) == "x"
    assert m.pop(OWNER, now) == "x"
