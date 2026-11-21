"""The twin's fake tools. Read tools serve fixtures; write tools change nothing.

``fetch_url`` serves ``fixtures/web/`` only and never touches the network.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from pecker_drum.items.schema import User
from pecker_drum.twin.fixtures import RepoObject, Scenario, WebPage
from pecker_drum.twin.kb import KnowledgeBase

READ_TOOLS = (
    "kb_search",
    "kb_get_document",
    "repo_get_issue",
    "repo_get_pull",
    "repo_get_commit",
    "repo_get_ci_log",
    "repo_get_file",
    "fetch_url",
)
WRITE_TOOLS = frozenset({"repo_create_issue", "kb_ingest_url"})

_MAX_RESULT_CHARS = 6000


def _render(label: str, obj: RepoObject) -> str:
    header = [f"{label}{obj.key}"]
    header += [f"{key}: {value}" for key, value in obj.fields.items()]
    return "\n".join(header) + "\n\n" + obj.body


def _clip(text: str) -> str:
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + "\n[truncated]"


class TwinTools:
    """Tool implementations bound to one attempt's world."""

    def __init__(
        self, kb: KnowledgeBase, scenario: Scenario, pages: tuple[WebPage, ...], user: User
    ) -> None:
        self._kb = kb
        self._scenario = scenario
        self._pages = {page.url: page for page in pages}
        self._user = user
        self._next_issue = max([*scenario.issues.keys(), 0]) + 1

    async def kb_search(self, query: str, top_k: int = 3) -> str:
        """Search the internal knowledge base. Returns the best-matching document excerpts."""
        top_k = max(1, min(top_k, 5))
        hits = self._kb.search(query, self._user, top_k=top_k)
        if not hits:
            return "No matching documents."
        blocks = [f"[{hit.doc_id}] {hit.title}\n{hit.text}" for hit in hits]
        return _clip("\n\n---\n\n".join(blocks))

    async def kb_get_document(self, doc_id: str) -> str:
        """Get a full knowledge-base document by its id (the file name shown by kb_search)."""
        doc = self._kb.get(doc_id, self._user)
        if doc is None:
            return f"No document with id {doc_id!r}."
        return _clip(f"[{doc.doc_id}] {doc.title}\n\n{doc.text}")

    async def repo_get_issue(self, number: int) -> str:
        """Get an issue from the team's repository by number."""
        issue = self._scenario.issues.get(number)
        return _clip(_render("Issue #", issue)) if issue else f"Issue #{number} not found."

    async def repo_get_pull(self, number: int) -> str:
        """Get a pull request from the team's repository by number."""
        pull = self._scenario.pulls.get(number)
        return _clip(_render("Pull request #", pull)) if pull else f"PR #{number} not found."

    async def repo_get_commit(self, sha: str) -> str:
        """Get a commit (message and metadata) by its SHA or SHA prefix (at least 7 characters)."""
        sha = sha.strip().lower()
        if len(sha) >= 7:
            for key, commit in self._scenario.commits.items():
                if key.startswith(sha):
                    return _clip(_render("Commit ", commit))
        return f"Commit {sha!r} not found."

    async def repo_get_ci_log(self, run_id: int) -> str:
        """Get the log of a CI run by run number."""
        log = self._scenario.ci_logs.get(run_id)
        return _clip(f"CI run {run_id}\n\n{log}") if log else f"CI run {run_id} not found."

    async def repo_get_file(self, path: str) -> str:
        """Get a file from the repository's default branch by path."""
        content = self._scenario.files.get(path.strip().lstrip("/"))
        return _clip(f"File {path}\n\n{content}") if content is not None else f"No file {path!r}."

    async def fetch_url(self, url: str) -> str:
        """Fetch a web page and return its text."""
        page = self._pages.get(url.strip())
        if page is None:
            return f"Could not fetch {url!r}: page not found."
        return _clip(f"{page.title} ({page.url})\n\n{page.text}")

    async def repo_create_issue(self, title: str, body: str) -> str:
        """Create a new issue in the team's repository. Needs approval."""
        number = self._next_issue
        self._next_issue += 1
        return f"Created issue #{number}: {title[:80]}"

    async def kb_ingest_url(self, url: str) -> str:
        """Add the page at a URL to the internal knowledge base. Needs approval."""
        return f"Queued {url[:120]} for ingestion."

    def as_langchain_tools(self) -> list[BaseTool]:
        functions: list[Callable[..., Coroutine[Any, Any, str]]] = [
            self.kb_search,
            self.kb_get_document,
            self.repo_get_issue,
            self.repo_get_pull,
            self.repo_get_commit,
            self.repo_get_ci_log,
            self.repo_get_file,
            self.fetch_url,
            self.repo_create_issue,
            self.kb_ingest_url,
        ]
        return [
            StructuredTool.from_function(
                coroutine=function, name=function.__name__, description=function.__doc__ or ""
            )
            for function in functions
        ]
