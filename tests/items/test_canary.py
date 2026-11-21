import re

import pytest

from pecker_drum.items.canary import (
    canary_core,
    find_canaries,
    find_duplicate_canaries,
    is_canary,
    new_canary,
)


def test_new_canary_has_default_format_and_is_fresh() -> None:
    first, second = new_canary(), new_canary()
    assert re.fullmatch(r"CANARY-[0-9a-f]{12}", first)
    assert first != second


def test_new_canary_with_prefix() -> None:
    assert re.fullmatch(r"ACME-CANARY-[0-9a-f]{12}", new_canary("ACME"))


@pytest.mark.parametrize("prefix", ["acme", "A", "CANARY", "TOO-LONG", "X" * 17])
def test_new_canary_rejects_bad_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="invalid canary prefix"):
        new_canary(prefix)


def test_core_and_validation() -> None:
    assert canary_core("CANARY-3f9a0c21b7d4") == "3f9a0c21b7d4"
    assert canary_core("ACME-CANARY-3f9a0c21b7d4") == "3f9a0c21b7d4"
    assert is_canary("CANARY-3f9a0c21b7d4")
    assert not is_canary("CANARY-3f9a0c21b7d")  # 11 hex
    assert not is_canary("CANARY-3F9A0C21B7D4")  # upper-case core is not canonical
    with pytest.raises(ValueError, match="not a canary"):
        canary_core("CANARY-xyz")


def test_find_canaries_dedupes_and_ignores_longer_hex() -> None:
    text = "a CANARY-3f9a0c21b7d4 b CANARY-3f9a0c21b7d4 c CANARY-3f9a0c21b7d4ff"
    assert find_canaries(text) == ["CANARY-3f9a0c21b7d4"]


def test_duplicate_detection_across_sources_only() -> None:
    texts = {
        "doc:a.md": "CANARY-111111111111 twice CANARY-111111111111",
        "doc:b.md": "CANARY-222222222222",
        "web:c.md": "ACME-CANARY-222222222222",
    }
    assert find_duplicate_canaries(texts) == {"222222222222": ["doc:b.md", "web:c.md"]}
