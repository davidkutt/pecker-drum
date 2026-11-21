"""Normalise text before matching, so encoded or disguised canaries still count.

Steps: Unicode NFKC, strip invisible and control characters, fold a few look-alike letters,
collapse separators (compact form), the reversed compact form, and decodings of base64, hex,
percent-encoded and HTML-escaped tokens (two levels deep).
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

# Invisible characters that are not in category Cf/Cc but still render as nothing.
_EXTRA_INVISIBLE = frozenset(
    {
        "͏",  # combining grapheme joiner
        "ᅟ",  # hangul choseong filler
        "ᅠ",  # hangul jungseong filler
        "ㅤ",  # hangul filler
        "ﾠ",  # halfwidth hangul filler
        "⠀",  # braille pattern blank
    }
)
_KEEP_CONTROL = frozenset({"\n", "\t"})

# Look-alikes for the characters a canary core is made of (0-9, a-f).
_CONFUSABLES = str.maketrans(
    {
        0x0430: "a",  # cyrillic a
        0x0441: "c",  # cyrillic es
        0x0435: "e",  # cyrillic ie
        0x0501: "d",  # cyrillic komi de
        0x03BF: "o",  # greek omicron
        0x043E: "o",  # cyrillic o
    }
)

_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")
_HEX_TOKEN = re.compile(r"(?:[0-9A-Fa-f]{2}[ :,\-]?){6,}")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WHITESPACE = re.compile(r"\s+")
_MAX_DEPTH = 2
_MAX_DECODED = 256


def _is_invisible(char: str) -> bool:
    if char in _KEEP_CONTROL:
        return False
    if char in _EXTRA_INVISIBLE:
        return True
    category = unicodedata.category(char)
    if category in {"Cf", "Cc", "Co", "Cn", "Cs"}:
        return True
    code = ord(char)
    # Variation selectors render as nothing on their own.
    return 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF


def strip_invisible(text: str) -> str:
    """Drop zero-width, bidi, tag, private-use and control characters (keeps newline, tab)."""
    return "".join(char for char in text if not _is_invisible(char))


def clean(text: str) -> str:
    """NFKC, then strip invisible characters, then fold look-alike letters."""
    return strip_invisible(unicodedata.normalize("NFKC", text)).translate(_CONFUSABLES)


def compact(text: str) -> str:
    """Case-folded text with every separator removed: ``3f 9a-0c`` -> ``3f9a0c``."""
    return _NON_ALNUM.sub("", text.casefold())


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    printable = sum(1 for char in text if char.isprintable() or char in "\n\t")
    return printable / len(text)


def _decode_bytes(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 4 or _printable_ratio(text) < 0.9:
        return None
    return text


def _decode_base64(token: str) -> str | None:
    stripped = token.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    for altchars in (b"+/", b"-_"):
        try:
            raw = base64.b64decode(padded, altchars=altchars, validate=True)
        except (binascii.Error, ValueError):
            continue
        decoded = _decode_bytes(raw)
        if decoded is not None:
            return decoded
    return None


def _decode_hex(token: str) -> str | None:
    digits = re.sub(r"[ :,\-]", "", token)
    if len(digits) % 2:
        digits = digits[:-1]
    try:
        raw = bytes.fromhex(digits)
    except ValueError:
        return None
    return _decode_bytes(raw)


def decodings(text: str) -> list[str]:
    """Texts hidden in ``text`` as base64, hex, percent-encoding or HTML entities."""
    found: list[str] = []
    seen: set[str] = {text}
    frontier = [text]
    for _ in range(_MAX_DEPTH):
        next_frontier: list[str] = []
        for current in frontier:
            candidates: list[str] = []
            if "%" in current:
                candidates.append(unquote(current))
            if "&" in current:
                candidates.append(html.unescape(current))
            candidates.extend(
                decoded
                for match in _BASE64_TOKEN.finditer(current)
                if (decoded := _decode_base64(match.group(0))) is not None
            )
            candidates.extend(
                decoded
                for match in _HEX_TOKEN.finditer(current)
                if (decoded := _decode_hex(match.group(0))) is not None
            )
            for candidate in candidates:
                candidate = clean(candidate)
                if candidate not in seen and len(found) < _MAX_DECODED:
                    seen.add(candidate)
                    found.append(candidate)
                    next_frontier.append(candidate)
        frontier = next_frontier
    return found


@dataclass(frozen=True, slots=True)
class NormalisedText:
    raw: str
    clean: str
    """NFKC, invisible characters stripped, look-alikes folded."""
    texts: tuple[str, ...]
    """``clean``, its whitespace-collapsed form and every decoding (for regex markers)."""
    compact_forms: tuple[str, ...]
    """Compact and reversed-compact form of every text (for canary cores)."""

    def contains_core(self, core: str) -> bool:
        needle = core.casefold()
        return any(needle in form for form in self.compact_forms)

    def search(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        for text in self.texts:
            match = pattern.search(text)
            if match is not None:
                return match
        return None


def normalise(text: str) -> NormalisedText:
    cleaned = clean(text)
    texts = [cleaned, collapse_whitespace(cleaned), *decodings(cleaned)]
    unique_texts = tuple(dict.fromkeys(texts))
    compact_forms: list[str] = []
    for item in unique_texts:
        form = compact(item)
        compact_forms.extend((form, form[::-1]))
    return NormalisedText(
        raw=text,
        clean=cleaned,
        texts=unique_texts,
        compact_forms=tuple(dict.fromkeys(compact_forms)),
    )
