"""Guard against invisible characters: no Unicode format (Cf) or control (Cc) characters.

Tab, LF and CR are allowed. AGENTS.md asks for escapes instead (write ``\\u200b``, not the
character). One checker for every place the rule applies (M1.1, M1.3, M1.6):

- ``files [ROOT]``: every file git would commit in the work tree ROOT (tracked files plus untracked
  files git does not ignore, ``git ls-files --cached --others --exclude-standard``) outside
  ``fixtures/`` and ``items/``, plus ``fixtures/README.md`` by its exact path. Fixtures and items
  hold attack text with such characters on purpose. ``tests/test_text_files.py`` runs the same
  check in pytest.
- ``commits BASE HEAD``: every commit message in ``BASE..HEAD`` (CI, on pull requests).
- ``pr-text``: the pull request title and body from the environment variables ``PR_TITLE`` and
  ``PR_BODY``. CI passes them through ``env:``, never inside the shell command. The body of a pull
  request whose ``PR_AUTHOR`` is ``dependabot[bot]`` is skipped: it quotes third-party release
  notes, where emoji sequences carry U+200D ZERO WIDTH JOINER. Its title and commits are still
  checked.

How ``files`` treats what is not plain UTF-8 text:

- Lock files such as ``uv.lock`` are UTF-8 text and scanned like any other file.
- A file that is not UTF-8 is a finding, unless it also contains a NUL byte: then it is binary and
  skipped. Skipped files are named in the output, and ``tests/test_text_files.py`` pins the set of
  skipped files, so a new binary file fails the test until its path is added on purpose (UTF-16
  text contains NUL bytes too and would otherwise slip through). Valid UTF-8 with a NUL byte is
  text, and the NUL is reported as U+0000.
- Symbolic links and other entries that are not regular files are skipped and never followed (a
  tracked link could point outside the work tree); they count as skipped files too.
- A tracked file deleted from the work tree is left out without an error.
- If git is missing or ROOT is not the top level of a git work tree, the scan raises an error; it
  never scans nothing in silence.

Each finding names where it is, the line and the code point, for example
``docs/notes.md:2: U+200B ZERO WIDTH SPACE``. Exit code 0 means clean, 1 means findings, 2 means
the scan could not run.

Dev-only: this script is not part of the ``pecker_drum`` package and uses the standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

FLAGGED_CATEGORIES = frozenset({"Cf", "Cc"})
ALLOWED = frozenset("\t\n\r")
SKIP_BODY_AUTHORS = frozenset({"dependabot[bot]"})
"""Pull request authors whose body quotes third-party text (checked through ``PR_AUTHOR``)."""
EXCLUDED_DIRS = ("fixtures/", "items/")
"""Top-level folders that hold attack text on purpose; ``files`` leaves them out."""
INCLUDED_ANYWAY = frozenset({"fixtures/README.md"})
"""Exact paths inside ``EXCLUDED_DIRS`` that are scanned all the same (docs, not fixture data)."""
REPO_ROOT = Path(__file__).resolve().parents[1]


def text_findings(label: str, text: str) -> list[str]:
    """One ``label:line: U+XXXX NAME`` entry per format or control character in ``text``."""
    found: list[str] = []
    for number, line in enumerate(text.split("\n"), start=1):
        for char in line:
            category = unicodedata.category(char)
            if char in ALLOWED or category not in FLAGGED_CATEGORIES:
                continue
            name = unicodedata.name(char, "control character" if category == "Cc" else "unnamed")
            found.append(f"{label}:{number}: U+{ord(char):04X} {name}")
    return found


def in_scope(rel: str) -> bool:
    """Whether the ``files`` rule covers ``rel`` (a POSIX path relative to the work tree root)."""
    return rel in INCLUDED_ANYWAY or not rel.startswith(EXCLUDED_DIRS)


def candidate_paths(root: Path) -> list[str]:
    """Every path git would commit in the work tree ``root``, relative to it and sorted.

    Tracked files plus untracked files git does not ignore. Raises ``RuntimeError`` when git is
    missing, ``root`` is not inside a git work tree, or ``root`` is not its top level.
    """
    root = root.resolve()
    try:
        top = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
        listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip() if exc.stderr else ""
        msg = f"{root} is not a git work tree ({detail or f'git exit code {exc.returncode}'})"
        raise RuntimeError(msg) from exc
    if Path(top).resolve() != root:
        msg = f"{root} is not the top level of its git work tree ({top})"
        raise RuntimeError(msg)
    return sorted({rel for rel in listed.decode("utf-8").split("\0") if rel})


def text_files(root: Path) -> list[Path]:
    """The files the ``files`` rule covers in ``root``: in scope and present in the work tree."""
    root = root.resolve()
    paths = [root / rel for rel in candidate_paths(root) if in_scope(rel)]
    # A tracked file deleted from the work tree is not there to scan (a dangling link still is).
    return [path for path in paths if path.is_symlink() or path.exists()]


def skip_reason(path: Path) -> str | None:
    """Why ``path`` is not scanned as text, or ``None`` when it is."""
    if path.is_symlink():
        return "symbolic link, not followed"
    if not path.is_file():
        return "not a regular file"
    raw = path.read_bytes()
    if b"\0" not in raw:
        return None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary: not UTF-8 and contains NUL bytes"
    return None


def findings(path: Path, root: Path) -> list[str]:
    """Findings for one file, labelled with its path relative to ``root``; non-UTF-8 is one."""
    rel = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"{rel}: not UTF-8 ({exc.reason} at byte {exc.start})"]
    return text_findings(rel, text)


@dataclass(frozen=True, slots=True)
class TreeScan:
    """The result of ``scan_tree``: findings, and the files skipped with the reason for each."""

    findings: list[str] = field(default_factory=list[str])
    skipped: dict[str, str] = field(default_factory=dict[str, str])


def scan_tree(root: Path) -> TreeScan:
    """Scan every file the ``files`` rule covers in the git work tree ``root``."""
    root = root.resolve()
    scan = TreeScan()
    for path in text_files(root):
        reason = skip_reason(path)
        if reason is None:
            scan.findings.extend(findings(path, root))
        else:
            scan.skipped[path.relative_to(root).as_posix()] = reason
    return scan


def message_findings(label: str, raw: bytes) -> list[str]:
    """Findings for a commit message given as raw bytes."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{label}: not UTF-8 ({exc.reason} at byte {exc.start})"]
    return text_findings(label, text)


def _git(repo: Path, *args: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        msg = "git not found"
        raise RuntimeError(msg)
    return subprocess.run(  # noqa: S603 - fixed git subcommands, arguments are never a shell string
        [git, "-C", str(repo), *args], check=True, capture_output=True
    ).stdout


def commit_findings(repo: Path, base: str, head: str) -> list[str]:
    """Findings in every commit message of ``base..head``, oldest commit first."""
    shas = _git(repo, "rev-list", "--reverse", f"{base}..{head}", "--").decode("ascii").split()
    found: list[str] = []
    for sha in shas:
        raw = _git(repo, "cat-file", "commit", sha)
        _headers, _, message = raw.partition(b"\n\n")
        found += message_findings(f"commit {sha[:12]}", message)
    return found


def pr_findings(title: str, body: str, author: str = "") -> list[str]:
    """Findings in a pull request title and body (the body is skipped for Dependabot)."""
    found = text_findings("PR title", title)
    if author not in SKIP_BODY_AUTHORS:
        found += text_findings("PR body", body)
    return found


def _report(found: Sequence[str], what: str) -> int:
    if not found:
        print(f"text guard: {what}: clean")
        return 0
    print(f"text guard: {what}: format or control characters, write them as escapes:")
    for line in found:
        print(f"  {line}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    files = sub.add_parser(
        "files", help="scan every file git would commit outside fixtures/ and items/"
    )
    files.add_argument("root", nargs="?", type=Path, default=REPO_ROOT)
    commits = sub.add_parser("commits", help="scan every commit message in BASE..HEAD")
    commits.add_argument("base")
    commits.add_argument("head")
    commits.add_argument("--repo", type=Path, default=Path.cwd())
    sub.add_parser("pr-text", help="scan PR_TITLE and PR_BODY from the environment")
    args = parser.parse_args(argv)

    if args.command == "files":
        try:
            scan = scan_tree(args.root)
        except RuntimeError as exc:
            print(f"text guard: files: cannot scan: {exc}", file=sys.stderr)
            return 2
        for rel, reason in scan.skipped.items():
            print(f"text guard: files: skipped {rel} ({reason})")
        return _report(scan.findings, "files")
    if args.command == "commits":
        found = commit_findings(args.repo, args.base, args.head)
        return _report(found, f"commits {args.base[:12]}..{args.head[:12]}")
    author = os.environ.get("PR_AUTHOR", "")
    if author in SKIP_BODY_AUTHORS:
        print(f"text guard: PR body skipped for {author} (third-party release notes)")
    found = pr_findings(os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""), author)
    return _report(found, "PR title and body")


if __name__ == "__main__":
    sys.exit(main())
