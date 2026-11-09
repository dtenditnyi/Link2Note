"""Digest → Markdown-заметка для Obsidian (шаблон templates/note.md.j2, структура PLAN §5).

Пустые разделы не выводятся. Строки во frontmatter экранируются как YAML (JSON-строка —
валидная YAML-строка в двойных кавычках), `|` в таблице — как `\\|`, код с ``` — длинным забором.
"""

import json
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from digest.classifier import youtube_id
from digest.schema import Digest

_TEMPLATES = Path(__file__).parent / "templates"
_SOURCE_LABELS = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "video": "Видео",
    "article": "Статья",
}
_RESOURCE_ICONS = {
    "book": "📖",
    "tool": "🛠",
    "site": "🌐",
    "video": "🎬",
    "course": "🎓",
}


def format_ts(seconds: int) -> str:
    h, rest = divmod(max(0, seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _oneline(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _cell(value: str) -> str:
    return _oneline(value).replace("|", "\\|")


def _keycell(value: str) -> str:
    value = _cell(value)
    if not value:
        return "—"
    return f"`` {value} ``" if "`" in value else f"`{value}`"


def _yaml_str(value: str) -> str:
    return json.dumps(_oneline(value), ensure_ascii=False)


def _fence(code: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,  # это Markdown, не HTML
)
_env.filters.update(
    oneline=_oneline,
    cell=_cell,
    keycell=_keycell,
    yq=_yaml_str,
    resource_icon=lambda kind: _RESOURCE_ICONS.get(kind, "🔗"),
)


def to_markdown(
    d: Digest,
    *,
    url: str,
    source: str,
    author: str,
    date_added: date,
    duration: int = 0,
    model: str = "",
) -> str:
    """duration — секунды (0 — неизвестна, не выводится). У статьи таймкодов не бывает."""
    vid = youtube_id(url)
    timestamps = []
    for t in [] if source == "article" else d.timestamps:
        if t.seconds < 0:
            continue
        ts = format_ts(t.seconds)
        text = f"[{ts}](https://youtu.be/{vid}?t={t.seconds})" if vid else ts
        timestamps.append({"text": text, "label": _oneline(t.label)})

    code = [
        {
            "fence": _fence(c.code),
            "language": _oneline(c.language),
            "body": c.code.strip("\n"),
            "explanation": _oneline(c.explanation),
        }
        for c in d.code
        if c.code.strip()
    ]

    return _env.get_template("note.md.j2").render(
        d=d,
        url=url,
        source=source,
        source_label=_SOURCE_LABELS.get(source, source),
        author=_oneline(author),
        duration=format_ts(duration) if duration > 0 else "",
        model=_oneline(model),
        date_added=date_added,
        tldr_lines=[line.strip() for line in d.tldr.strip().splitlines() if line.strip()],
        code=code,
        timestamps=timestamps,
    )
