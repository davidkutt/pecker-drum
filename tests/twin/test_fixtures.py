from pathlib import Path

import pytest

from pecker_drum.items.schema import Item, User
from pecker_drum.twin.fixtures import FixtureError, FixtureLibrary, parse_front_matter


def make_item(*fixtures: str) -> Item:
    return Item.model_validate(
        {
            "id": "t-001",
            "title": "t",
            "vector": "indirect_tool",
            "lang": "en",
            "severity": "low",
            "user": {"sub": "carol"},
            "fixtures": list(fixtures),
            "turns": ["hi"],
            "succeeds_if": [{"kind": "canary_in_answer"}],
            "origin": "hand",
            "self_test": {},
        }
    )


def test_front_matter() -> None:
    assert parse_front_matter("---\na: 1\nb: x, y\n---\n\nbody\n") == (
        {"a": "1", "b": "x, y"},
        "body\n",
    )
    assert parse_front_matter("no front matter") == ({}, "no front matter")
    with pytest.raises(FixtureError, match="not closed"):
        parse_front_matter("---\na: 1\n")
    with pytest.raises(FixtureError, match="bad front-matter"):
        parse_front_matter("---\nnot a pair\n---\n")


def test_world_overlays_item_fixtures(mini_fixtures: Path) -> None:
    library = FixtureLibrary(mini_fixtures)
    world = library.world_for(make_item("scenario:demo", "doc:runbook-cache.md", "web:incident.md"))
    assert sorted(doc.doc_id for doc in world.documents) == [
        "onboarding.md",
        "runbook-cache.md",
        "salaries.md",
    ]
    assert sorted(world.scenario.issues) == [1, 10]
    assert "9c1e4b7aa" in world.scenario.commits
    assert world.scenario.ci_logs[5150].startswith("step 1")
    assert ".github/workflows/deploy.yml" in world.scenario.files
    assert {page.url for page in world.pages} == {
        "https://docs.twin.example/",
        "https://status.example/incidents/1",
    }
    assert set(world.fixture_texts) == {"scenario:demo", "doc:runbook-cache.md", "web:incident.md"}


def test_base_world_has_no_item_fixtures(mini_fixtures: Path) -> None:
    world = FixtureLibrary(mini_fixtures).world_for(make_item())
    assert sorted(world.scenario.issues) == [1]
    assert world.fixture_texts == {}


def test_visibility(mini_fixtures: Path) -> None:
    docs = {doc.doc_id: doc for doc in FixtureLibrary(mini_fixtures).base_documents()}
    assert docs["onboarding.md"].visible_to(User(sub="carol"))
    assert not docs["salaries.md"].visible_to(User(sub="carol"))
    assert docs["salaries.md"].visible_to(User(sub="hana", groups=("hr",)))


def test_missing_and_escaping_fixtures_are_errors(mini_fixtures: Path) -> None:
    library = FixtureLibrary(mini_fixtures)
    with pytest.raises(FixtureError, match="not found"):
        library.document("nope.md")
    with pytest.raises(FixtureError, match="escapes"):
        library.document("../../outside.md")
    with pytest.raises(FixtureError, match="not found"):
        library.scenario("nope")


def test_web_page_needs_url(mini_fixtures: Path) -> None:
    (mini_fixtures / "web" / "bad.md").write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    with pytest.raises(FixtureError, match="url"):
        FixtureLibrary(mini_fixtures).page("bad.md")


def test_all_fixture_texts_lists_every_unit(mini_fixtures: Path) -> None:
    refs = set(FixtureLibrary(mini_fixtures).all_fixture_texts())
    assert refs == {
        "doc:base/onboarding.md",
        "doc:base/salaries.md",
        "doc:runbook-cache.md",
        "scenario:base",
        "scenario:demo",
        "web:base/home.md",
        "web:incident.md",
    }
