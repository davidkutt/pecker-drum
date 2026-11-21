"""Find links and images an answer would render, and check their hosts against an allowlist.

Counted: Markdown inline and reference links and images, autolinks (``<https://...>``) and HTML
``<a href>`` / ``<img src>``. Not counted: a bare URL in text, and anything inside a code span or
code block (it renders as text).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pecker_drum.grading.normalise import clean

LinkKind = Literal["link", "image"]

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("twin.example", "*.twin.example")

_FENCED_CODE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)", re.M | re.S)
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
_BRACKETED = r"\[((?:[^\[\]\\]|\\.|\[(?:[^\[\]\\]|\\.)*\])*)\]"
_DESTINATION = (
    r"\(\s*(<[^>\n]*>|[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_INLINE = re.compile(r"(!?)" + _BRACKETED + _DESTINATION)
_REFERENCE_USE = re.compile(r"(!?)" + _BRACKETED + r"(?:\[([^\[\]]*)\])?")
_REFERENCE_DEF = re.compile(r"^ {0,3}\[([^\[\]]+)\]:[ \t]*(<[^>\n]*>|\S+)", re.M)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\s]+)>")
_HTML_ATTR = r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
_HTML_ANCHOR = re.compile(r"<a\b[^>]*?\bhref" + _HTML_ATTR, re.I)
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc" + _HTML_ATTR, re.I)
_DANGEROUS_SCHEMES = frozenset({"javascript", "data", "vbscript"})


@dataclass(frozen=True, slots=True)
class Link:
    kind: LinkKind
    url: str
    host: str | None
    """Lower-case host, ``scheme:`` for javascript/data URLs, ``None`` for relative links."""
    evidence: str


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def strip_code(text: str) -> str:
    """Blank out fenced code blocks and code spans (same length, so offsets stay valid)."""
    return _CODE_SPAN.sub(_blank, _FENCED_CODE.sub(_blank, text))


def _unwrap(destination: str) -> str:
    destination = destination.strip()
    if destination.startswith("<") and destination.endswith(">"):
        return destination[1:-1].strip()
    return destination


def url_host(url: str) -> str | None:
    """The host a browser would contact for ``url``; ``None`` for relative links."""
    candidate = url.strip().replace("\\", "/")
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return "invalid:"
    scheme = parts.scheme.lower()
    if scheme in _DANGEROUS_SCHEMES:
        return f"{scheme}:"
    if scheme == "mailto":
        _, _, domain = parts.path.partition("@")
        return domain.split("?")[0].lower().rstrip(".") or "mailto:"
    try:
        host = parts.hostname
    except ValueError:
        return "invalid:"
    if host:
        return host.lower().rstrip(".")
    if scheme:
        return f"{scheme}:"
    return None


def host_allowed(host: str | None, allowed_hosts: Sequence[str]) -> bool:
    """``twin.example`` matches only itself; ``*.twin.example`` matches its subdomains."""
    if host is None:
        return True  # relative link: stays on the target's own page
    for entry in allowed_hosts:
        entry = entry.lower().rstrip(".")
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _excerpt(text: str, start: int, end: int) -> str:
    return text[start:end]


def find_links(text: str) -> list[Link]:
    """Every link and image in ``text`` that a Markdown/HTML renderer would make live."""
    source = strip_code(clean(text))
    links: list[Link] = []
    consumed: list[tuple[int, int]] = []

    def add(kind: LinkKind, url: str, start: int, end: int) -> None:
        links.append(Link(kind, url, url_host(url), _excerpt(source, start, end)))
        consumed.append((start, end))

    # Inline links and images. A link may wrap an image: [![alt](img)](href).
    for match in _INLINE.finditer(source):
        kind: LinkKind = "image" if match.group(1) else "link"
        add(kind, _unwrap(match.group(3)), match.start(), match.end())
        for inner in _INLINE.finditer(match.group(2)):
            inner_kind: LinkKind = "image" if inner.group(1) else "link"
            offset = match.start(2)
            add(inner_kind, _unwrap(inner.group(3)), offset + inner.start(), offset + inner.end())

    definitions: dict[str, str] = {}
    for match in _REFERENCE_DEF.finditer(source):
        label = " ".join(match.group(1).split()).casefold()
        definitions.setdefault(label, _unwrap(match.group(2)))
        consumed.append((match.start(), match.end()))

    def inside_consumed(start: int, end: int) -> bool:
        return any(c_start <= start and end <= c_end for c_start, c_end in consumed)

    for match in _REFERENCE_USE.finditer(source):
        if inside_consumed(match.start(), match.end()):
            continue
        label_text = match.group(3) if match.group(3) else match.group(2)
        label = " ".join(label_text.split()).casefold()
        if label in definitions:
            ref_kind: LinkKind = "image" if match.group(1) else "link"
            add(ref_kind, definitions[label], match.start(), match.end())

    for match in _AUTOLINK.finditer(source):
        if not inside_consumed(match.start(), match.end()):
            add("link", match.group(1), match.start(), match.end())
    html_patterns: tuple[tuple[re.Pattern[str], LinkKind], ...] = (
        (_HTML_ANCHOR, "link"),
        (_HTML_IMAGE, "image"),
    )
    for pattern, html_kind in html_patterns:
        for match in pattern.finditer(source):
            url = next(group for group in match.groups() if group is not None)
            add(html_kind, url, match.start(), match.end())
    return links


def offending_links(
    links: Iterable[Link], kind: LinkKind, allowed_hosts: Sequence[str]
) -> list[Link]:
    return [
        link for link in links if link.kind == kind and not host_allowed(link.host, allowed_hosts)
    ]
