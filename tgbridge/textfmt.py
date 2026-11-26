"""Safe formatting for Telegram: a small Markdown subset -> Telegram HTML,
HTML escaping, and splitting long text into <=4096-character parts.

Telegram counts message length in UTF-16 code units, so we do too.
"""

from __future__ import annotations

import html
import re

TG_LIMIT = 4096
SOURCE_CHUNK = 3800


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def escape_html(s: str) -> str:
    return html.escape(s, quote=False)


def _escape_attr(s: str) -> str:
    return html.escape(s, quote=True)


_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?|tg)://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UND_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_SPOILER_RE = re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", re.S)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def md_to_html(src: str) -> str:
    """Convert a conservative Markdown subset to Telegram HTML.

    Everything that is not recognised markup is HTML-escaped, so arbitrary
    text (including <, >, &) is always safe to send with parse_mode=HTML.
    """
    holders: list[str] = []

    def hold(fragment: str) -> str:
        holders.append(fragment)
        return f"\x00{len(holders) - 1}\x00"

    def fence(m: re.Match) -> str:
        lang, code = m.group(1), m.group(2).rstrip("\n")
        cls = f' class="language-{_escape_attr(lang)}"' if lang else ""
        return hold(f"<pre><code{cls}>{escape_html(code)}</code></pre>")

    s = src.replace("\x00", "")
    s = _FENCE_RE.sub(fence, s)
    s = _INLINE_CODE_RE.sub(lambda m: hold(f"<code>{escape_html(m.group(1))}</code>"), s)
    s = _LINK_RE.sub(
        lambda m: hold(f'<a href="{_escape_attr(m.group(2))}">{escape_html(m.group(1))}</a>'), s
    )
    s = escape_html(s)
    s = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _STRIKE_RE.sub(r"<s>\1</s>", s)
    s = _SPOILER_RE.sub(r"<tg-spoiler>\1</tg-spoiler>", s)
    s = _ITALIC_STAR_RE.sub(r"<i>\1</i>", s)
    s = _ITALIC_UND_RE.sub(r"<i>\1</i>", s)

    # Block quotes: consecutive lines starting with "&gt; " (escaped ">").
    out_lines: list[str] = []
    quote: list[str] = []
    for line in s.split("\n"):
        if line.startswith("&gt; ") or line == "&gt;":
            quote.append(line[5:] if line.startswith("&gt; ") else "")
            continue
        if quote:
            out_lines.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote = []
        out_lines.append(line)
    if quote:
        out_lines.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
    s = "\n".join(out_lines)

    return re.sub(r"\x00(\d+)\x00", lambda m: holders[int(m.group(1))], s)


def _cut_point(text: str, limit: int) -> int:
    """Largest index <= limit (in UTF-16 units) at a friendly boundary."""
    # Map UTF-16 limit to a Python index.
    idx, units = 0, 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > limit:
            break
        units += w
        idx += 1
    if idx >= len(text):
        return len(text)
    window = text[:idx]
    for sep in ("\n\n", "\n", ". ", " "):
        pos = window.rfind(sep)
        if pos > idx // 3:
            return pos + len(sep)
    return idx


def split_plain(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts: list[str] = []
    rest = text
    while rest:
        if utf16_len(rest) <= limit:
            parts.append(rest)
            break
        cut = _cut_point(rest, limit)
        parts.append(rest[:cut].rstrip("\n") or rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return [p for p in parts if p.strip()] or ([text[:limit]] if text else [])


def split_markdown(src: str, limit: int = SOURCE_CHUNK) -> list[str]:
    """Split Markdown source, keeping ``` code fences balanced in each part."""
    chunks = split_plain(src, limit - 8)
    fixed: list[str] = []
    open_lang: str | None = None
    for chunk in chunks:
        prefix = f"```{open_lang}\n" if open_lang is not None else ""
        body = prefix + chunk
        fences = re.findall(r"```([A-Za-z0-9_+-]*)", body)
        lang = None
        inside = False
        for f in fences:
            inside = not inside
            if inside:
                lang = f
        if inside:
            body += "\n```"
            open_lang = lang or ""
        else:
            open_lang = None
        fixed.append(body)
    return fixed


def render_parts(text: str, fmt: str) -> list[tuple[str, str | None]]:
    """Return [(text, parse_mode)] parts, each within Telegram's limit."""
    if fmt == "plain":
        return [(p, None) for p in split_plain(text)]
    if fmt == "html":
        parts = split_plain(text)
        return [(p, "HTML") for p in parts]
    # markdown (default)
    out: list[tuple[str, str | None]] = []
    for chunk in split_markdown(text):
        rendered = md_to_html(chunk)
        if utf16_len(rendered) <= TG_LIMIT:
            out.append((rendered, "HTML"))
        else:  # heavy escaping made it too long: fall back to plain for this chunk
            out.extend((p, None) for p in split_plain(chunk))
    return out
