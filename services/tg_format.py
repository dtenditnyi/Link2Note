"""Markdown ответа ИИ → безопасный HTML Telegram, порезанный под лимит сообщения.

Telegram понимает только <b> <i> <code> <pre> <a> и т.п.; любой другой «<» сломает разбор
(или станет разметкой). Поэтому сначала экранируем всё, потом превращаем в теги только
известные конструкции Markdown. Ссылки и код прячем до разбора *курсива*, итог проверяем на
вложенность тегов — разметка «вперехлёст» превращается в простой текст, а не в ошибку Telegram.
Режем по абзацам и по длине уже экранированного текста — тег никогда не разрывается.
"""

import re
from html import escape, unescape

TG_LIMIT = 4096
_SAFE = 3800  # запас под теги, которые добавит форматирование

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s<>]+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(
    r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])"
)
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_BULLET_RE = re.compile(r"(?m)^(\s*)[-*]\s+")
_TAG_RE = re.compile(r"<(/?)(b|i|code|pre|a)(?:\s[^>]*)?>")


def _balanced(html: str) -> bool:
    stack: list[str] = []
    for m in _TAG_RE.finditer(html):
        closing, tag = m.group(1), m.group(2)
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return False
    return not stack


def _inline(text: str) -> str:
    """Экранированный текст → теги. Код и ссылки прячем: их *звёздочки* — не курсив."""
    kept: list[str] = []

    def keep(html: str) -> str:
        kept.append(html)
        return f"\x00{len(kept) - 1}\x00"

    text = _CODE_RE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)
    text = _LINK_RE.sub(
        lambda m: keep(f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>'),
        text,
    )
    text = _URL_RE.sub(lambda m: keep(m.group(0)), text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def md_to_html(md: str) -> str:
    out = []
    pos = 0
    for m in _FENCE_RE.finditer(md):
        out.append(_inline(escape(md[pos : m.start()], quote=False)))
        out.append(f"<pre>{escape(m.group(1).rstrip(), quote=False)}</pre>")
        pos = m.end()
    out.append(_inline(escape(md[pos:], quote=False)))
    html = "".join(out).strip()
    return html if _balanced(html) else escape(md, quote=False).strip()


def _blocks(md: str) -> list[str]:
    """Абзацы; блок ``` целиком — один кусок."""
    blocks, pos = [], 0
    for m in _FENCE_RE.finditer(md):
        blocks += [b for b in re.split(r"\n\s*\n", md[pos : m.start()]) if b.strip()]
        blocks.append(m.group(0))
        pos = m.end()
    blocks += [b for b in re.split(r"\n\s*\n", md[pos:]) if b.strip()]
    return blocks


def _esc_len(text: str) -> int:
    return len(escape(text, quote=False))


def _split_escaped(raw: str, size: int) -> list[str]:
    """Куски raw; длина каждого ПОСЛЕ экранирования ≤ size. Режем по строкам, иначе по символам."""
    pieces: list[str] = []
    cur = ""
    for line in raw.split("\n"):
        candidate = f"{cur}\n{line}" if cur else line
        if _esc_len(candidate) <= size:
            cur = candidate
            continue
        if cur:
            pieces.append(cur)
            cur = ""
        while _esc_len(line) > size:
            n, used = 0, 0
            for ch in line:
                step = _esc_len(ch)
                if used + step > size:
                    break
                used += step
                n += 1
            pieces.append(line[:n])
            line = line[n:]
        cur = line
    if cur:
        pieces.append(cur)
    return [p for p in pieces if p]


def split_markdown(md: str, limit: int = TG_LIMIT) -> list[str]:
    """Куски HTML, каждый ≤ limit символов и с закрытыми тегами."""
    size = min(_SAFE, limit - 200)
    pieces: list[str] = []
    for block in _blocks(md):
        if len(md_to_html(block)) <= size:
            pieces.append(block)
            continue
        inner = _FENCE_RE.fullmatch(block.strip())
        raw = inner.group(1) if inner else block
        for part in _split_escaped(raw, size - 20):  # 20 — на <pre></pre>
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
    out: list[str] = []
    for chunk in chunks:  # страховка: кусок длиннее лимита — простым текстом, без тегов
        if len(chunk) <= limit:
            out.append(chunk)
        else:
            plain = unescape(re.sub(r"<[^>]+>", "", chunk))
            out += [escape(p, quote=False) for p in _split_escaped(plain, limit)]
    return [c for c in out if c]
