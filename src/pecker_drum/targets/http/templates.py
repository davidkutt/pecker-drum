"""Plain ``{name}`` substitution for request paths, JSON bodies and form fields (M2).

No ``str.format`` and no template engine: a template may contain ``{name}`` with a lower-case
name from a fixed set per request, and ``{{`` / ``}}`` for literal braces. Nothing else in braces
is accepted, so there is no attribute access (``{x.y}``), indexing or format spec. Values put into
a URL path are percent-encoded completely, so a hostile id from a target response (``../admin``,
``x?y``, ``//evil``) stays inside one path segment. JSON bodies are templated value by value, so a
substituted value is always one JSON string and can never change the body's structure.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from urllib.parse import quote

from pydantic import JsonValue

_TOKEN = re.compile(r"\{\{|\}\}|\{([a-z][a-z0-9_]*)\}|[{}]")
_LITERALS = {"{{": "{", "}}": "}"}


class TemplateError(ValueError):
    """A template uses an unknown variable or a stray brace."""


def placeholders(template: str) -> set[str]:
    """The variable names in ``template``; raises on a stray ``{`` or ``}``."""
    names: set[str] = set()
    for match in _TOKEN.finditer(template):
        piece = match.group(0)
        if piece in _LITERALS:
            continue
        name = match.group(1)
        if name is None:
            msg = f"stray {piece!r} in {template!r} (write {{{{ or }}}} for a literal brace)"
            raise TemplateError(msg)
        names.add(name)
    return names


def check(template: str, allowed: Collection[str], where: str) -> None:
    """Raise ``TemplateError`` if ``template`` uses a variable outside ``allowed``."""
    unknown = sorted(placeholders(template) - set(allowed))
    if unknown:
        msg = (
            f"{where}: unknown variable {', '.join('{' + n + '}' for n in unknown)} "
            f"(allowed here: {', '.join('{' + n + '}' for n in sorted(allowed))})"
        )
        raise TemplateError(msg)


def render(template: str, values: Mapping[str, str], *, quote_values: bool = False) -> str:
    """Substitute ``values`` into ``template``. A missing value renders as the empty string."""

    def substitute(match: re.Match[str]) -> str:
        piece = match.group(0)
        if piece in _LITERALS:
            return _LITERALS[piece]
        name = match.group(1)
        if name is None:  # pragma: no cover - templates are checked when the config loads
            msg = f"stray {piece!r} in {template!r}"
            raise TemplateError(msg)
        value = values.get(name, "")
        return quote(value, safe="") if quote_values else value

    return _TOKEN.sub(substitute, template)


def render_path(template: str, values: Mapping[str, str]) -> str:
    """A URL path with every substituted value percent-encoded (``/`` included)."""
    return render(template, values, quote_values=True)


def json_strings(value: JsonValue) -> Iterator[str]:
    """Every string value in a JSON body template (keys are never templated)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for element in value:
            yield from json_strings(element)
    elif isinstance(value, dict):
        for element in value.values():
            yield from json_strings(element)


def render_json(value: JsonValue, values: Mapping[str, str]) -> JsonValue:
    """Substitute ``values`` into every string of a JSON body template."""
    if isinstance(value, str):
        return render(value, values)
    if isinstance(value, list):
        return [render_json(element, values) for element in value]
    if isinstance(value, dict):
        return {key: render_json(element, values) for key, element in value.items()}
    return value
