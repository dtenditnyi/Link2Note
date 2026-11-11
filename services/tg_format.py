"""Markdown ответа ИИ → безопасный HTML Telegram, порезанный под лимит сообщения.

Telegram понимает только <b> <i> <code> <pre> <a> и т.п.; любой другой «<» сломает разбор
(или станет разметкой). Поэтому сначала экранируем всё, потом превращаем в теги только
известные конструкции Markdown. Режем по абзацам (блок кода не разрываем, пока он влезает),
каждый кусок форматируем отдельно — теги никогда не остаются открытыми между сообщениями.
"""

import re
from html import escape

TG_LIMIT = 4096
_SAFE = 3800  # запас: после форматирования текст длиннее исходника (&lt; и теги)

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(
    r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"
)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_BULLET_RE = re.compile(r"(?m)^(\s*)[-*]\s+")


def _inline(text: str) -> str:
    """Экранированный текст → теги. Код прячем, чтобы *звёздочки* внутри не стали курсивом."""
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_RE.sub(keep, text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def md_to_html(md: str) -> str:
    out = []
    pos = 0
    for m in _FENCE_RE.finditer(md):
        out.append(_inline(escape(md[pos : m.start()], quote=False)))
        out.append(f"<pre>{escape(m.group(1).rstrip(), quote=False)}</pre>")
        pos = m.end()
    out.append(_inline(escape(md[pos:], quote=False)))
    return "".join(out).strip()


def _blocks(md: str) -> list[str]:
    """Абзацы; блок ``` целиком — один кусок."""
    blocks, pos = [], 0
    for m in _FENCE_RE.finditer(md):
        blocks += [b for b in re.split(r"\n\s*\n", md[pos : m.start()]) if b.strip()]
        blocks.append(m.group(0))
        pos = m.end()
    blocks += [b for b in re.split(r"\n\s*\n", md[pos:]) if b.strip()]
    return blocks


def _hard_split(block: str, size: int) -> list[str]:
    lines, pieces, cur = block.splitlines(), [], ""
    for line in lines:
        while len(line) > size:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size and cur:
            pieces.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        pieces.append(cur)
    return pieces


def split_markdown(md: str, limit: int = TG_LIMIT) -> list[str]:
    """Куски HTML, каждый ≤ limit символов."""
    size = min(_SAFE, limit - 200)
    pieces: list[str] = []
    for block in _blocks(md):
        if len(md_to_html(block)) <= size:
            pieces.append(block)
        else:  # огромный абзац / блок кода — по строкам, без блочной разметки
            inner = _FENCE_RE.fullmatch(block.strip())
            raw = inner.group(1) if inner else block
            for part in _hard_split(raw, size // 2):
                pieces.append(f"```\n{part}\n```" if inner else part)
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        candidate = f"{cur}\n\n{piece}" if cur else piece
        if cur and len(md_to_html(candidate)) > size:
            chunks.append(md_to_html(cur))
            cur = piece
        else:
            cur = candidate
    if cur:
        chunks.append(md_to_html(cur))
    return [c[:limit] for c in chunks if c]
