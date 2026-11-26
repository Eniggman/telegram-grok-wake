"""Telegram Rich Messages (Bot API 10.1–10.4).

The official API accepts content as Markdown, HTML, or explicit blocks
(https://core.telegram.org/bots/api#rich-messages). This module validates the
documented limits and builds an InputRichMessage. Markdown is passed to
Telegram as-is — no third-party converter is needed (or vendored): GFM tables,
headings, lists, and mixed-in HTML such as <details> are part of the official
Rich Markdown dialect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.types import InputRichMessage

# Official limits: https://core.telegram.org/bots/api#rich-message-limits
RICH_MAX_CHARS = 32768          # UTF-8 characters, incl. formula source / emoji alt
RICH_MAX_BLOCKS = 500           # incl. nested blocks, list items, table rows, details
RICH_MAX_NESTING = 16
RICH_MAX_MEDIA = 50
RICH_MAX_TABLE_COLS = 20

# A GFM table separator row: |---|:---:|---:|
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_MD_ROW = re.compile(r"^\s*\|.+\|\s*$")
_HEADING = re.compile(r"^#{1,6}\s+\S", re.M)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)]|- \[[ xX]\])\s+\S", re.M)
_HR = re.compile(r"^(?:[-*_]\s*){3,}$", re.M)
_FENCE = re.compile(r"```.*?```", re.S)
_MD_MEDIA = re.compile(r"!\[[^\]]*\]\(\s*\S+")
_HTML_MEDIA = re.compile(
    r"<(?:img|video|audio|source|tg-document|tg-map)\b", re.I)
_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.I | re.S)
_HTML_TR = re.compile(r"<tr\b[^>]*>.*?</tr>", re.I | re.S)
_HTML_CELL = re.compile(r"</t[dh]\s*>", re.I)
_DETAILS_OPEN = re.compile(r"<details\b", re.I)
_DETAILS_CLOSE = re.compile(r"</details\s*>", re.I)
_BQ_LINE = re.compile(r"^>\s?", re.M)


@dataclass(frozen=True)
class RichLimit:
    """One failed (or close) documented limit."""
    message: str
    hard: bool = True  # hard = must not call sendRichMessage with this payload


class RichLimitError(ValueError):
    """The payload is over a documented Rich Message limit."""

    def __init__(self, issues: list[RichLimit]):
        self.issues = issues
        ValueError.__init__(self, "; ".join(i.message for i in issues))


def _cols_in_md_row(line: str) -> int:
    return len([c for c in line.strip().strip("|").split("|")])


def _table_column_counts(text: str) -> list[int]:
    counts: list[int] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        if _MD_ROW.match(lines[i]) and _TABLE_SEP.match(lines[i + 1]):
            n = _cols_in_md_row(lines[i])
            i += 2
            while i < len(lines) and _MD_ROW.match(lines[i]):
                n = max(n, _cols_in_md_row(lines[i]))
                i += 1
            counts.append(n)
            continue
        i += 1
    for table in _HTML_TABLE.findall(text):
        first = _HTML_TR.search(table)
        if first:
            counts.append(len(_HTML_CELL.findall(first.group(0))))
    return counts


def _media_count(text: str) -> int:
    return len(_MD_MEDIA.findall(text)) + len(_HTML_MEDIA.findall(text))


def _max_details_depth(text: str) -> int:
    depth = peak = 0
    for m in re.finditer(r"</?details\b", text, re.I):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth < 0:
            depth = 0
        peak = max(peak, depth)
    return peak


def estimate_blocks(text: str) -> int:
    """A conservative lower bound of how many rich blocks the text will produce.

    Under-counting is preferred: the API is the authority, and a rejected
    sendRichMessage falls back to sendMessage. Over-counting would refuse a
    valid message.
    """
    stripped = _FENCE.sub("```\n```", text)
    rows = 0
    for n in _table_column_counts(stripped):
        # header + separator counted as header row + one row per body line,
        # already folded into n of columns; recount rows properly below.
        del n
    # table rows (header + body, not the separator)
    lines = stripped.splitlines()
    i = 0
    table_rows = 0
    while i < len(lines) - 1:
        if _MD_ROW.match(lines[i]) and _TABLE_SEP.match(lines[i + 1]):
            table_rows += 1  # header
            i += 2
            while i < len(lines) and _MD_ROW.match(lines[i]):
                table_rows += 1
                i += 1
            continue
        i += 1
    for table in _HTML_TABLE.findall(stripped):
        table_rows += len(_HTML_TR.findall(table))
    headings = len(_HEADING.findall(stripped))
    items = len(_LIST_ITEM.findall(stripped))
    hrs = len(_HR.findall(stripped))
    details = len(_DETAILS_OPEN.findall(stripped))
    fences = len(re.findall(r"```", text)) // 2
    media = _media_count(text)
    quotes = 0
    in_q = False
    for line in stripped.splitlines():
        if line.startswith(">"):
            if not in_q:
                quotes += 1
                in_q = True
        else:
            in_q = False
    # remaining paragraphs: blank-line separated chunks that aren't already counted
    paras = len(re.findall(r"(?:^|\n\n+)[^\n#>\-\*\+|<\s].+", stripped))
    return max(1, table_rows + headings + items + hrs + details + fences + media + quotes + paras)


_HTML_HEADING = re.compile(r"<h[1-6]\b", re.I)
_ATX = re.compile(r"^(#{1,6})\s+\S", re.M)


def looks_rich(text: str) -> bool:
    """True when the text uses constructs that only Rich Messages render natively:
    ATX headings (`#`–`######`), GFM or HTML tables, `<details>`, or `<h1>`–`<h6>`.

    Bold, italic, code, links and lists do **not** qualify — classic sendMessage
    already renders those. Fenced code is ignored so a pasted example does not
    force Rich.
    """
    if not text:
        return False
    visible = _FENCE.sub("", text)
    if _DETAILS_OPEN.search(visible) or _HTML_TABLE.search(visible) or _HTML_HEADING.search(visible):
        return True
    if _ATX.search(visible):
        return True
    lines = visible.splitlines()
    for i, line in enumerate(lines[:-1]):
        if _MD_ROW.match(line) and _TABLE_SEP.match(lines[i + 1]):
            return True
    return False


def should_send_rich(text: str, *, rich: bool = False, classic: bool = False, fmt: str = "md") -> bool:
    """Which path `tg-send text` should take.

    - `--format plain` is never Rich.
    - `--rich` always requests Rich (wins over `--classic` if both are passed).
    - `--classic` forces the classic sendMessage path (disables auto-detect).
    - otherwise auto-detect via looks_rich().
    """
    if fmt == "plain":
        return False
    if rich:
        return True
    if classic:
        return False
    return looks_rich(text)


def inspect_rich(text: str) -> list[RichLimit]:
    """Return every limit that this payload violates (empty = ok)."""
    issues: list[RichLimit] = []
    n = len(text)
    if n > RICH_MAX_CHARS:
        issues.append(RichLimit(
            f"rich message is {n} characters, over the limit of {RICH_MAX_CHARS}"))
    cols = _table_column_counts(text)
    for c in cols:
        if c > RICH_MAX_TABLE_COLS:
            issues.append(RichLimit(
                f"a table has {c} columns, over the limit of {RICH_MAX_TABLE_COLS}"))
    media = _media_count(text)
    if media > RICH_MAX_MEDIA:
        issues.append(RichLimit(
            f"message has {media} media items, over the limit of {RICH_MAX_MEDIA}"))
    blocks = estimate_blocks(text)
    if blocks > RICH_MAX_BLOCKS:
        issues.append(RichLimit(
            f"message looks like {blocks} blocks, over the limit of {RICH_MAX_BLOCKS}"))
    depth = _max_details_depth(text)
    if depth > RICH_MAX_NESTING:
        issues.append(RichLimit(
            f"details nesting is {depth} levels, over the limit of {RICH_MAX_NESTING}"))
    return issues


def validate_rich(text: str) -> None:
    """Raise RichLimitError if a documented hard limit is exceeded."""
    issues = inspect_rich(text)
    if issues:
        raise RichLimitError(issues)


def build_rich_message(text: str, *, html: bool = False) -> InputRichMessage:
    """Exactly one of html / markdown, as required by InputRichMessage."""
    body = text if text is not None else ""
    if html:
        return InputRichMessage(html=body)
    return InputRichMessage(markdown=body)
