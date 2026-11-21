"""Canaries: unique markers planted in fixtures so a leak can be traced to its source.

Format: ``CANARY-<12 hex>``; private sets may put their target's prefix in front:
``<PREFIX>-CANARY-<12 hex>``. Matching uses only the 12-hex core.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping

CANARY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:([A-Z][A-Z0-9]{1,15})-)?CANARY-([0-9a-f]{12})(?![0-9a-f])"
)
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")


def new_canary(prefix: str | None = None) -> str:
    """Return a fresh canary. Never reuse one across fixtures or set versions."""
    core = secrets.token_hex(6)
    if prefix is None:
        return f"CANARY-{core}"
    if not _PREFIX_RE.fullmatch(prefix) or prefix == "CANARY":
        msg = f"invalid canary prefix {prefix!r}: use 2-16 upper-case letters or digits"
        raise ValueError(msg)
    return f"{prefix}-CANARY-{core}"


def is_canary(value: str) -> bool:
    """True if ``value`` is exactly one well-formed canary."""
    match = CANARY_RE.fullmatch(value)
    return match is not None


def canary_core(canary: str) -> str:
    """The 12-hex core of a canary (what the grader matches on)."""
    match = CANARY_RE.fullmatch(canary)
    if match is None:
        msg = f"not a canary: {canary!r}"
        raise ValueError(msg)
    return match.group(2)


def find_canaries(text: str) -> list[str]:
    """All canaries that appear literally in ``text``, in order, without duplicates."""
    seen: dict[str, None] = {}
    for match in CANARY_RE.finditer(text):
        seen.setdefault(match.group(0), None)
    return list(seen)


def find_duplicate_canaries(texts: Mapping[str, str]) -> dict[str, list[str]]:
    """Map each canary core that appears in more than one source to those sources.

    ``texts`` maps a source id (for example ``doc:runbook.md``) to its full text. A canary may
    repeat inside one source but must not appear in two.
    """
    owners: dict[str, list[str]] = {}
    for source_id, text in texts.items():
        for canary in find_canaries(text):
            sources = owners.setdefault(canary_core(canary), [])
            if source_id not in sources:
                sources.append(source_id)
    return {core: sources for core, sources in owners.items() if len(sources) > 1}
