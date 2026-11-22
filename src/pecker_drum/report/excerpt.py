"""Escaped hostile text for reports: short excerpts and full answers.

Every excerpt is at most 120 characters after escaping. Invisible and control characters are shown
as ``\\uXXXX`` escapes, and the characters that could break out of a Markdown code span or table
cell (backtick, pipe, backslash) are escaped too, so nothing in a report renders as a live link or
image.

Full answers (M1.5, ADR 0008) keep their line breaks and are shown only inside a fenced code block
whose fence is longer than any backtick run in the text, so nothing in them renders either.
"""

from __future__ import annotations

import re
import unicodedata

EXCERPT_LIMIT = 120
_ELLIPSIS = "…"
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\\": "\\\\", "`": "\\x60", "|": "\\x7c"}


def _escape_char(char: str, *, inline: bool = True) -> str:
    if char in _NAMED and (inline or char not in "`|"):
        return _NAMED[char]
    category = unicodedata.category(char)
    if category in {"Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp"} or (category == "Zs" and char != " "):
        code = ord(char)
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    code = ord(char)
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return char


def escape_excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """Escape ``text`` and cut it to ``limit`` characters (an escape is never split)."""
    pieces: list[str] = []
    length = 0
    for char in text:
        piece = _escape_char(char)
        if length + len(piece) > limit - 1:
            pieces.append(_ELLIPSIS)
            return "".join(pieces)
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces)


def code_span(text: str) -> str:
    """Wrap an already escaped excerpt in a Markdown code span (renders as plain text)."""
    return f"`{text}`" if text else ""


_BACKTICK_RUN = re.compile(r"`+")


def escape_answer(text: str) -> str:
    """Escape a full answer: like an excerpt, but uncut, with real line breaks and with backticks
    and pipes kept (a fenced code block shows them as text). Backslashes are doubled so a literal
    ``\\u200b`` in the answer never looks like an escaped character."""
    return "".join("\n" if char == "\n" else _escape_char(char, inline=False) for char in text)


def fenced_block(text: str) -> str:
    """Wrap escaped text in a fenced code block that nothing inside it can close.

    The fence is one backtick longer than the longest backtick run in ``text`` (at least three).
    A closing fence needs at least as many backticks as the opening one, so no line of ``text`` can
    end the block, and Markdown, links, images and HTML inside it stay plain text.
    """
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"
