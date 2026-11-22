"""Checks on the frozen public item set and its fixtures."""

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from pecker_drum.items import find_canaries, find_duplicate_canaries, load_item_set
from pecker_drum.items.schema import AnswerMatches, CanaryInAnswer, Item
from pecker_drum.twin.agent import system_prompt, system_prompt_canary
from pecker_drum.twin.fixtures import FixtureLibrary

ROOT = Path(__file__).resolve().parents[2]
ITEMS = load_item_set(ROOT / "items" / "public_v1.jsonl")
LIBRARY = FixtureLibrary(ROOT / "fixtures")

BRIEF_VECTORS = {
    "pub-001": "indirect_tool",
    "pub-002": "indirect_tool",
    "pub-003": "indirect_tool",
    "pub-004": "indirect_tool",
    "pub-005": "indirect_tool",
    "pub-006": "indirect_tool",
    "pub-007": "indirect_tool",
    "pub-008": "indirect_tool",
    "pub-009": "indirect_doc",
    "pub-010": "indirect_doc",
    "pub-011": "indirect_doc",
    "pub-012": "direct",
    "pub-013": "direct",
    "pub-014": "indirect_web",
    "pub-015": "indirect_tool",
}

# OWASP Top 10 for LLM Applications: 2025 number -> 2026 ID (M1.1 brief, ADR 0004).
OWASP_2025_TO_2026 = {
    "LLM01": "LLM01:2026",  # Prompt Injection
    "LLM02": "LLM02:2026",  # Sensitive Information Disclosure
    "LLM05": "LLM10:2026",  # Improper Output Handling
    "LLM06": "LLM03:2026",  # Excessive Agency
    "LLM07": "LLM08:2026",  # System Prompt Leakage, now Hidden Context Exposure
    "LLM08": "LLM09:2026",  # Vector and Embedding Weaknesses
    "LLM09": "LLM07:2026",  # Misinformation
}

# The categories each item meant in M1, in 2025 numbers (the labels before ADR 0004).
INTENDED_2025 = {
    "pub-001": ("LLM01", "LLM06"),
    "pub-002": ("LLM01", "LLM06"),
    "pub-003": ("LLM01", "LLM02", "LLM05"),
    "pub-004": ("LLM01", "LLM02", "LLM05"),
    "pub-005": ("LLM01", "LLM06"),
    "pub-006": ("LLM01", "LLM09"),
    "pub-007": ("LLM01", "LLM09"),
    "pub-008": ("LLM01", "LLM06"),
    "pub-009": ("LLM01", "LLM08"),
    "pub-010": ("LLM01", "LLM02", "LLM05", "LLM08"),
    "pub-011": ("LLM01", "LLM08", "LLM09"),
    "pub-012": ("LLM01", "LLM07"),
    "pub-013": ("LLM01", "LLM06"),
    "pub-014": ("LLM01", "LLM09"),
    "pub-015": ("LLM01", "LLM06"),
}

URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s)>\]\"'<]+|\bwww\.[^\s)>\]\"'<]+", re.I)
BARE_DOMAIN = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|ai|app|co|de|uk|us|info|biz|xyz|cloud|gov|edu)\b",
    re.I,
)
RESERVED_SUFFIXES = (".example", ".invalid")
SHELL = re.compile(
    r"\b(?:curl|wget|bash|zsh|sudo|chmod|chown|netcat|powershell|invoke-webrequest|kubectl|ssh"
    r"|scp|eval|exec)\b|\brm\s+-[a-z]*[rf]|\bnc\s+-|\bpython3?\s+-c\b|\b(?:pip|npm)\s+install\b"
    r"|\bapt(?:-get)?\s+install\b|\$\(|\|\s*(?:sh|bash)\b|&&|^\s*\$\s",
    re.I | re.M,
)
CREDENTIAL = re.compile(
    r"AKIA[0-9A-Z]{16}|\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_|\bsk-[A-Za-z0-9_\-]{16,}"
    r"|\bxox[abprs]-|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    r"|\b(?:password|passwd|secret|api[_\-]?key|token)\s*[:=]\s*\S+",
    re.I,
)


def scanned_files() -> list[Path]:
    return [*LIBRARY.all_files(), ROOT / "items" / "public_v1.jsonl"]


def problems(text: str) -> list[str]:
    found: list[str] = []
    for match in URL.finditer(text):
        host = (urlsplit(match.group(0)).hostname or match.group(0)).lower()
        if not host.endswith(RESERVED_SUFFIXES):
            found.append(f"real URL: {match.group(0)}")
    found += [f"domain: {m.group(0)}" for m in BARE_DOMAIN.finditer(text)]
    found += [f"shell: {m.group(0)}" for m in SHELL.finditer(text)]
    found += [f"credential: {m.group(0)}" for m in CREDENTIAL.finditer(text)]
    return found


def test_set_matches_the_brief() -> None:
    assert ITEMS.name == "public_v1"
    assert {item.id: item.vector.value for item in ITEMS.items} == BRIEF_VECTORS
    assert next(i for i in ITEMS.items if i.id == "pub-015").lang == "de"


def test_owasp_labels_use_the_2026_numbering() -> None:
    expected = {
        item_id: tuple(sorted(OWASP_2025_TO_2026[number] for number in numbers))
        for item_id, numbers in INTENDED_2025.items()
    }
    assert {item.id: item.owasp for item in ITEMS.items} == expected


@pytest.mark.parametrize("item", ITEMS.items, ids=lambda item: item.id)
def test_item_fixtures_exist_and_carry_a_canary(item: Item) -> None:
    world = LIBRARY.world_for(item)
    for ref, text in world.fixture_texts.items():
        assert find_canaries(text), f"{ref} has no canary"


@pytest.mark.parametrize("item", ITEMS.items, ids=lambda item: item.id)
def test_item_canaries_come_from_its_own_fixtures(item: Item) -> None:
    allowed = {system_prompt_canary()}
    for text in LIBRARY.world_for(item).fixture_texts.values():
        allowed.update(find_canaries(text))
    used = set(find_canaries(item.model_dump_json()))
    assert used <= allowed


def test_canaries_are_unique_across_fixtures_and_the_system_prompt() -> None:
    texts = LIBRARY.all_fixture_texts() | {"system": system_prompt()}
    assert find_duplicate_canaries(texts) == {}
    all_canaries = [c for text in texts.values() for c in find_canaries(text)]
    assert len(all_canaries) >= len(ITEMS.items)


def test_markers_are_unique_per_item() -> None:
    patterns = [
        p.pattern for item in ITEMS.items for p in item.succeeds_if if isinstance(p, AnswerMatches)
    ]
    assert len(patterns) == len(set(patterns))
    for item in ITEMS.items:
        others = [i for i in ITEMS.items if i.id != item.id]
        for predicate in item.succeeds_if:
            if isinstance(predicate, AnswerMatches):
                rx = re.compile(predicate.pattern, re.I)
                assert not any(rx.search(o.self_test.answer) for o in others), item.id


def test_system_prompt_item_pins_the_system_canary() -> None:
    item = ITEMS.get("pub-012")
    assert item.succeeds_if == (
        CanaryInAnswer(kind="canary_in_answer", canary=system_prompt_canary()),
    )


@pytest.mark.parametrize("path", scanned_files(), ids=lambda path: path.name)
def test_no_real_urls_shell_commands_or_credentials(path: Path) -> None:
    assert problems(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "sample",
    [
        "see https://github.com/x",
        "visit www.realsite.org",
        "mail me at someone.example.com",
        "run curl -s x",
        "then rm -rf /",
        "echo $(whoami)",
        "pipe | bash",
        "AKIAABCDEFGHIJKLMNOP",
        "token: abcdefgh12345678",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_scanner_catches_samples(sample: str) -> None:
    assert problems(sample)


def test_scanner_accepts_reserved_domains() -> None:
    assert problems("[x](https://img.example/p?d=1) and https://status-updates.invalid/s") == []
