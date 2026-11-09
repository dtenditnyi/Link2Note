import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from digest.render import format_ts, to_markdown
from digest.schema import CodeBlock, Digest, Resource, Shortcut, Timestamp

GOLDEN = Path(__file__).parent / "golden"
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
DAY = date(2026, 11, 9)


def _minimal(**kw) -> Digest:
    data = {
        "title": "Короткое видео",
        "emoji": "🎬",
        "tldr": "Суть.",
        "key_points": ["Пункт"],
        "tags": ["dev"],
        "read_minutes": 1,
        "confidence": "medium",
    }
    data.update(kw)
    return Digest.model_validate(data)


def _full() -> Digest:
    return Digest(
        title="10 горячих клавиш VS Code",
        emoji="⌨️",
        tldr="Мультикурсор и перемещение строк — 80% выигрыша.",
        key_points=["Мультикурсор экономит больше всего", "Перемещение строк без копирования"],
        steps=["Выделить слово", "Нажать `Ctrl+Shift+L`"],
        shortcuts=[
            Shortcut(action="Курсор на все совпадения", windows="Ctrl+Shift+L", mac="Cmd+Shift+L"),
            Shortcut(action="Переместить строку вверх", windows="Alt+Up", mac=""),
        ],
        code=[
            CodeBlock(
                language="bash",
                code="code --install-extension vscodevim.vim",
                explanation="Установить расширение:",
            ),
            CodeBlock(language="python", code='print("hi")\n', explanation=""),
        ],
        resources=[
            Resource(kind="book", name="Clean Code", note="про именование"),
            Resource(kind="tool", name="VS Code", note=""),
        ],
        action_items=["Попробовать мультикурсор на лабе по OOP"],
        timestamps=[
            Timestamp(seconds=5, label="вступление"),
            Timestamp(seconds=135, label="мультикурсор"),
        ],
        tags=["dev", "obsidian"],
        read_minutes=3,
        confidence="high",
    )


def _frontmatter(md: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    assert m, "нет frontmatter"
    return yaml.safe_load(m.group(1))


def test_r1_golden_full():
    md = to_markdown(_full(), url=URL, source="youtube", author="Fireship", date_added=DAY)
    assert md == (GOLDEN / "full.md").read_text(encoding="utf-8")


def test_r2_minimal_has_no_empty_sections():
    md = to_markdown(_minimal(), url=URL, source="youtube", author="", date_added=DAY)
    for heading in (
        "Горячие клавиши",
        "Пошагово",
        "Команды и код",
        "Упомянутые ресурсы",
        "Что сделать",
        "Таймкоды",
    ):
        assert heading not in md
    assert "## 🔑 Главное" in md
    assert "\n\n\n" not in md
    assert md.endswith("\n") and not md.endswith("\n\n")
    fm = _frontmatter(md)
    assert "author" not in fm  # пустой автор не выводится
    assert "duration" not in fm


def test_r3_yaml_and_markdown_escaping():
    d = _minimal(
        title='Fix: "quotes" & #tags',
        shortcuts=[Shortcut(action="Pipe | split", windows="Ctrl+|", mac="")],
        code=[CodeBlock(language="markdown", code="```py\nx = 1\n```", explanation="")],
    )
    md = to_markdown(d, url=URL, source="youtube", author='A: B "C"', date_added=DAY)
    fm = _frontmatter(md)
    assert fm["author"] == 'A: B "C"'
    assert fm["url"] == URL
    assert fm["tags"] == ["dev"]
    assert fm["date_added"] == DAY
    assert fm["status"] == "unread"
    assert fm["type"] == "digest"
    assert "| Pipe \\| split | `Ctrl+\\|` | — |" in md
    assert "````markdown\n```py\nx = 1\n```\n````" in md


def test_r3_multiline_tldr_stays_in_callout():
    md = to_markdown(
        _minimal(tldr="Строка 1\nСтрока 2"),
        url=URL,
        source="youtube",
        author="",
        date_added=DAY,
    )
    assert "> Строка 1\n> Строка 2" in md


def test_r3_newlines_in_title_flattened():
    md = to_markdown(
        _minimal(title="Раз\nДва"), url=URL, source="youtube", author="", date_added=DAY
    )
    assert "# 🎬 Раз Два\n" in md


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "00:00"), (5, "00:05"), (135, "02:15"), (3599, "59:59"), (3725, "1:02:05")],
)
def test_r4_format_ts(seconds, text):
    assert format_ts(seconds) == text


def test_r4_timestamp_links():
    d = _minimal(timestamps=[Timestamp(seconds=3725, label="конец")])
    md = to_markdown(d, url=URL, source="youtube", author="", date_added=DAY)
    assert "- [1:02:05](https://youtu.be/dQw4w9WgXcQ?t=3725) — конец" in md


ARTICLE = "https://blog.example.com/posts/vs-code-tips"
TIKTOK = "https://www.tiktok.com/@u/video/7301234567890123456"


def test_r5_article_golden():
    d = _minimal(
        title="Советы по VS Code",
        emoji="📖",
        tldr="Мультикурсор экономит больше всего времени.",
        key_points=["Ctrl+Shift+L ставит курсор на все совпадения"],
        resources=[Resource(kind="tool", name="VS Code")],
        action_items=["Попробовать мультикурсор"],
        timestamps=[Timestamp(seconds=10, label="не бывает у статей")],
        read_minutes=2,
        confidence="high",
    )
    md = to_markdown(d, url=ARTICLE, source="article", author="Иван Петров", date_added=DAY)
    assert md == (GOLDEN / "article.md").read_text(encoding="utf-8")


def test_r6_tiktok_duration_and_plain_timestamps():
    d = _minimal(timestamps=[Timestamp(seconds=135, label="главное")])
    md = to_markdown(d, url=TIKTOK, source="tiktok", author="u", date_added=DAY, duration=522)
    fm = _frontmatter(md)
    assert fm["duration"] == "08:42"
    assert fm["source"] == "tiktok"
    assert '\nduration: "08:42"\n' in md
    assert "- 02:15 — главное" in md
    assert "youtu.be" not in md
    assert md.endswith(f"🔗 Источник: [TikTok]({TIKTOK})\n")


@pytest.mark.parametrize(
    ("source", "label"),
    [("youtube", "YouTube"), ("instagram", "Instagram"), ("tiktok", "TikTok"), ("video", "Видео")],
)
def test_r6_source_labels(source, label):
    md = to_markdown(_minimal(), url=TIKTOK, source=source, author="", date_added=DAY)
    assert f"🔗 Источник: [{label}](" in md


def test_r6_youtube_duration_zero_not_shown():
    md = to_markdown(_minimal(), url=URL, source="youtube", author="", date_added=DAY, duration=0)
    assert "duration" not in _frontmatter(md)


def test_negative_timestamp_skipped():
    d = _minimal(timestamps=[Timestamp(seconds=-1, label="x")])
    md = to_markdown(d, url=URL, source="youtube", author="", date_added=DAY)
    assert "Таймкоды" not in md


def test_m1_model_in_frontmatter():
    md = to_markdown(
        _minimal(), url=URL, source="youtube", author="", date_added=DAY, model="gemini-3.7-flash"
    )
    assert _frontmatter(md)["model"] == "gemini-3.7-flash"
    assert "confidence: medium\nmodel: gemini-3.7-flash\n" in md


def test_m1_no_model_no_field():
    md = to_markdown(_minimal(), url=URL, source="youtube", author="", date_added=DAY)
    assert "model" not in _frontmatter(md)
