"""Load the twin's fixtures: documents, repo scenarios and web pages.

Layout (everything under ``fixtures/``)::

    documents/base/*.md        always in the knowledge base (benign)
    documents/<file>           loaded when an item lists ``doc:<file>``
    scenarios/base/            always in the fake repo (benign)
    scenarios/<name>/          overlaid when an item lists ``scenario:<name>``
        issues/<n>.md  pulls/<n>.md  commits/<sha>.md  ci/<run_id>.log  files/<path>
    web/base/*.md              always served by fetch_url (benign)
    web/<file>                 served when an item lists ``web:<file>``

Markdown fixtures may start with a front-matter block of ``key: value`` lines between ``---``
lines. List values are comma-separated. Fixture text is attacker-controlled data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pecker_drum.items.schema import Item, User

Visibility = Literal["public", "internal", "restricted"]
_VISIBILITIES: tuple[Visibility, ...] = ("public", "internal", "restricted")


class FixtureError(ValueError):
    """A fixture is missing or malformed."""


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` front matter from the body. Returns ``({}, text)`` if there is none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "".join(lines[index + 1 :]).lstrip("\n")
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            msg = f"bad front-matter line: {line.strip()!r}"
            raise FixtureError(msg)
        meta[key.strip()] = value.strip()
    msg = "front matter is not closed with ---"
    raise FixtureError(msg)


def _split_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    """File name, e.g. ``runbook-cache-restart.md``."""
    ref: str
    """Fixture reference (``doc:<file>``) or ``doc:base/<file>`` for the base corpus."""
    title: str
    text: str
    domains: tuple[str, ...] = ()
    visibility: Visibility = "internal"
    allowed_groups: tuple[str, ...] = ()

    def visible_to(self, user: User) -> bool:
        if self.visibility != "restricted":
            return True
        return bool(set(self.allowed_groups) & set(user.groups))


@dataclass(frozen=True, slots=True)
class RepoObject:
    """An issue, pull request or commit: front matter fields plus the body text."""

    key: str
    fields: Mapping[str, str]
    body: str


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    issues: Mapping[int, RepoObject] = field(default_factory=dict[int, RepoObject])
    pulls: Mapping[int, RepoObject] = field(default_factory=dict[int, RepoObject])
    commits: Mapping[str, RepoObject] = field(default_factory=dict[str, RepoObject])
    ci_logs: Mapping[int, str] = field(default_factory=dict[int, str])
    files: Mapping[str, str] = field(default_factory=dict[str, str])

    def overlay(self, other: Scenario) -> Scenario:
        """``other`` on top of ``self`` (other wins on the same key)."""
        return Scenario(
            name=other.name,
            issues={**self.issues, **other.issues},
            pulls={**self.pulls, **other.pulls},
            commits={**self.commits, **other.commits},
            ci_logs={**self.ci_logs, **other.ci_logs},
            files={**self.files, **other.files},
        )


@dataclass(frozen=True, slots=True)
class WebPage:
    ref: str
    url: str
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class ItemWorld:
    """Everything one attempt's twin can see."""

    documents: tuple[Document, ...]
    scenario: Scenario
    pages: tuple[WebPage, ...]
    fixture_texts: Mapping[str, str]
    """Fixture ref -> full text, for canary bookkeeping."""


class FixtureLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            msg = f"fixtures directory not found: {root}"
            raise FixtureError(msg)

    # -- paths ---------------------------------------------------------------------------

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            msg = f"fixture path escapes the fixtures directory: {path}"
            raise FixtureError(msg)
        return resolved

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    # -- documents -----------------------------------------------------------------------

    def _load_document(self, path: Path, ref: str) -> Document:
        meta, body = parse_front_matter(self._read(path))
        visibility = meta.get("visibility", "internal")
        if visibility not in _VISIBILITIES:
            msg = f"{path}: visibility must be one of {_VISIBILITIES}"
            raise FixtureError(msg)
        return Document(
            doc_id=path.name,
            ref=ref,
            title=meta.get("title", path.stem),
            text=body,
            domains=_split_list(meta.get("domains")),
            visibility=visibility,  # type: ignore[arg-type]  # checked above
            allowed_groups=_split_list(meta.get("allowed_groups")),
        )

    def base_documents(self) -> list[Document]:
        folder = self.root / "documents" / "base"
        return [
            self._load_document(path, f"doc:base/{path.name}")
            for path in sorted(folder.glob("*.md"))
        ]

    def document(self, name: str) -> Document:
        path = self._inside(self.root / "documents" / name)
        if not path.is_file():
            msg = f"document fixture not found: doc:{name}"
            raise FixtureError(msg)
        return self._load_document(path, f"doc:{name}")

    # -- scenarios -----------------------------------------------------------------------

    def _load_objects(self, folder: Path) -> dict[str, RepoObject]:
        objects: dict[str, RepoObject] = {}
        if not folder.is_dir():
            return objects
        for path in sorted(folder.glob("*.md")):
            meta, body = parse_front_matter(self._read(path))
            objects[path.stem] = RepoObject(key=path.stem, fields=meta, body=body)
        return objects

    @staticmethod
    def _numbered(objects: Mapping[str, RepoObject], kind: str) -> dict[int, RepoObject]:
        numbered: dict[int, RepoObject] = {}
        for key, value in objects.items():
            if not key.isdigit():
                msg = f"{kind} fixture names must be numbers, got {key!r}"
                raise FixtureError(msg)
            numbered[int(key)] = value
        return numbered

    def _load_scenario(self, folder: Path, name: str) -> Scenario:
        if not folder.is_dir():
            msg = f"scenario fixture not found: scenario:{name}"
            raise FixtureError(msg)
        ci_logs: dict[int, str] = {}
        ci_folder = folder / "ci"
        if ci_folder.is_dir():
            for path in sorted(ci_folder.glob("*.log")):
                if not path.stem.isdigit():
                    msg = f"CI log names must be run numbers, got {path.name!r}"
                    raise FixtureError(msg)
                ci_logs[int(path.stem)] = self._read(path)
        files: dict[str, str] = {}
        files_folder = folder / "files"
        if files_folder.is_dir():
            for path in sorted(files_folder.rglob("*")):
                if path.is_file():
                    files[path.relative_to(files_folder).as_posix()] = self._read(path)
        return Scenario(
            name=name,
            issues=self._numbered(self._load_objects(folder / "issues"), "issue"),
            pulls=self._numbered(self._load_objects(folder / "pulls"), "pull"),
            commits=self._load_objects(folder / "commits"),
            ci_logs=ci_logs,
            files=files,
        )

    def base_scenario(self) -> Scenario:
        return self._load_scenario(self.root / "scenarios" / "base", "base")

    def scenario(self, name: str) -> Scenario:
        folder = self._inside(self.root / "scenarios" / name)
        return self._load_scenario(folder, name)

    # -- web -----------------------------------------------------------------------------

    def _load_page(self, path: Path, ref: str) -> WebPage:
        meta, body = parse_front_matter(self._read(path))
        url = meta.get("url")
        if not url:
            msg = f"{path}: web fixtures need a url in their front matter"
            raise FixtureError(msg)
        return WebPage(ref=ref, url=url, title=meta.get("title", path.stem), text=body)

    def base_pages(self) -> list[WebPage]:
        folder = self.root / "web" / "base"
        return [
            self._load_page(path, f"web:base/{path.name}") for path in sorted(folder.glob("*.md"))
        ]

    def page(self, name: str) -> WebPage:
        path = self._inside(self.root / "web" / name)
        if not path.is_file():
            msg = f"web fixture not found: web:{name}"
            raise FixtureError(msg)
        return self._load_page(path, f"web:{name}")

    # -- items ---------------------------------------------------------------------------

    def world_for(self, item: Item) -> ItemWorld:
        """Base fixtures plus the item's own fixtures."""
        documents = self.base_documents()
        scenario = self.base_scenario()
        pages = self.base_pages()
        texts: dict[str, str] = {}
        for ref in item.fixtures:
            kind, _, name = ref.partition(":")
            if kind == "doc":
                documents.append(self.document(name))
            elif kind == "scenario":
                scenario = scenario.overlay(self.scenario(name))
            elif kind == "web":
                pages.append(self.page(name))
            texts[ref] = self.fixture_text(ref)
        return ItemWorld(tuple(documents), scenario, tuple(pages), texts)

    def fixture_text(self, ref: str) -> str:
        """The raw text of a fixture (all files of a scenario, concatenated)."""
        kind, _, name = ref.partition(":")
        if kind == "doc":
            return self._read(self._inside(self.root / "documents" / name))
        if kind == "web":
            return self._read(self._inside(self.root / "web" / name))
        if kind == "scenario":
            folder = self._inside(self.root / "scenarios" / name)
            return "\n".join(self._read(p) for p in sorted(folder.rglob("*")) if p.is_file())
        msg = f"unknown fixture kind in {ref!r}"
        raise FixtureError(msg)

    def all_fixture_texts(self) -> dict[str, str]:
        """Every fixture unit in the library, for the canary uniqueness check."""
        refs: list[str] = []
        documents = self.root / "documents"
        refs += [f"doc:base/{p.name}" for p in sorted((documents / "base").glob("*.md"))]
        refs += [f"doc:{p.name}" for p in sorted(documents.glob("*.md"))]
        scenarios = self.root / "scenarios"
        refs += [f"scenario:{p.name}" for p in sorted(scenarios.iterdir()) if p.is_dir()]
        web = self.root / "web"
        refs += [f"web:base/{p.name}" for p in sorted((web / "base").glob("*.md"))]
        refs += [f"web:{p.name}" for p in sorted(web.glob("*.md"))]
        return {ref: self.fixture_text(ref) for ref in refs}

    def all_files(self) -> Iterable[Path]:
        return (path for path in sorted(self.root.rglob("*")) if path.is_file())
