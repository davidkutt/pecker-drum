"""No invisible format or control characters in any text file git would commit (AGENTS.md, M1.6).

The rule covers every file git would commit (tracked files plus untracked files git does not
ignore) outside ``fixtures/`` and ``items/``, plus ``fixtures/README.md``. Fixtures and items may
hold such characters on purpose (attack text); nothing else does. Write them as escapes instead, for
example ``\\u200b``. The checker lives in ``tools/text_guard.py``; CI uses the same script for
commit messages and pull request text. The scope tests use throwaway git repositories in
``tmp_path``; every invisible character here is built from an escape, so this file stays clean.
"""

import subprocess
from pathlib import Path

import pytest
import text_guard
from text_guard import TreeScan, findings, main, scan_tree, text_files

ROOT = Path(__file__).resolve().parents[1]
ZWSP = "\u200b"
FOUND_ZWSP = "U+200B ZERO WIDTH SPACE"

EXPECTED_SKIPPED: frozenset[str] = frozenset()
"""Files in scope that the guard skips (binary files, links). Empty today.

A new binary file fails ``test_skipped_files_are_exactly_the_expected_set`` until its path is added
here on purpose: UTF-16 text contains NUL bytes too, so the binary rule alone would skip a UTF-16
file full of invisible characters without anyone noticing.
"""


def plant(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def plant_bytes(root: Path, rel: str, raw: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git arguments in a temporary repository
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty git work tree in ``tmp_path`` (nothing committed, nothing pushed)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


@pytest.fixture(scope="module")
def tree() -> TreeScan:
    return scan_tree(ROOT)


# --- this checkout -------------------------------------------------------------------------------


def test_whole_tree_has_no_format_or_control_characters(tree: TreeScan) -> None:
    assert not tree.findings, "format or control characters, write them as escapes:\n" + "\n".join(
        tree.findings
    )


def test_skipped_files_are_exactly_the_expected_set(tree: TreeScan) -> None:
    reasons = "\n".join(f"{rel}: {reason}" for rel, reason in sorted(tree.skipped.items()))
    assert set(tree.skipped) == EXPECTED_SKIPPED, (
        "skipped files changed; add a binary file to EXPECTED_SKIPPED on purpose:\n" + reasons
    )


def test_scope_covers_code_config_and_docs_but_not_fixture_data() -> None:
    scanned = {path.relative_to(ROOT).as_posix() for path in text_files(ROOT)}
    assert {
        "src/pecker_drum/cli.py",
        "tests/test_text_files.py",
        "tools/text_guard.py",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "uv.lock",
        "docs/spec.md",
        "docs/milestones/M1.6.md",
        "README.md",
        "AGENTS.md",
        "SECURITY.md",
        "fixtures/README.md",
    } <= scanned
    assert "fixtures/scenarios/base/files/README.md" not in scanned
    assert "items/public_v1.jsonl" not in scanned
    assert {rel for rel in scanned if rel.startswith(("fixtures/", "items/"))} == {
        "fixtures/README.md"
    }


# --- scope, in a throwaway repository -------------------------------------------------------------


def test_planted_zero_width_space_under_src_and_tests_is_reported(repo: Path) -> None:
    plant(repo, "src/pkg/mod.py", f'"""Module."""\n\nNAME = "split{ZWSP}word"\n')
    plant(repo, "tests/test_mod.py", f"# split{ZWSP}word\n")
    plant(repo, "tools/clean.py", "OK = 1\n")
    _git(repo, "add", "-A")
    assert scan_tree(repo).findings == [
        f"src/pkg/mod.py:3: {FOUND_ZWSP}",
        f"tests/test_mod.py:1: {FOUND_ZWSP}",
    ]


def test_untracked_file_is_scanned_unless_git_ignores_it(repo: Path) -> None:
    plant(repo, ".gitignore", "scratch/\n")
    _git(repo, "add", ".gitignore")
    text = f"x = 'split{ZWSP}word'\n"
    plant(repo, "tests/test_new.py", text)  # untracked, not ignored
    plant(repo, "scratch/tests/test_new.py", text)  # untracked, ignored
    assert scan_tree(repo).findings == [f"tests/test_new.py:1: {FOUND_ZWSP}"]


def test_fixtures_and_items_are_skipped_except_fixtures_readme(repo: Path) -> None:
    for rel in [
        "fixtures/README.md",
        "fixtures/scenarios/base/files/README.md",
        "fixtures/documents/x.md",
        "items/set.jsonl",
        "docs/a.md",
        "src/fixtures/a.py",
    ]:
        plant(repo, rel, f"attack{ZWSP}text\n")
    _git(repo, "add", "-A")
    assert scan_tree(repo).findings == [
        f"docs/a.md:1: {FOUND_ZWSP}",
        f"fixtures/README.md:1: {FOUND_ZWSP}",
        f"src/fixtures/a.py:1: {FOUND_ZWSP}",
    ]


def test_tracked_file_deleted_from_the_work_tree_is_left_out(repo: Path) -> None:
    plant(repo, "src/gone.py", f"x = '{ZWSP}'\n").parent.joinpath("kept.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    (repo / "src" / "gone.py").unlink()
    assert [path.relative_to(repo.resolve()).as_posix() for path in text_files(repo)] == [
        "src/kept.py"
    ]
    assert scan_tree(repo) == TreeScan()


def test_lock_file_is_scanned_as_text(repo: Path) -> None:
    plant(repo, "uv.lock", f'version = 1\n[[package]]\nname = "a{ZWSP}b"\n')
    _git(repo, "add", "-A")
    assert scan_tree(repo).findings == [f"uv.lock:3: {FOUND_ZWSP}"]


def test_binary_file_is_skipped_and_named(repo: Path) -> None:
    plant_bytes(repo, "docs/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff")
    _git(repo, "add", "-A")
    assert scan_tree(repo) == TreeScan(
        skipped={"docs/logo.png": "binary: not UTF-8 and contains NUL bytes"}
    )


def test_utf16_text_is_skipped_as_binary_so_the_expected_set_must_catch_it(repo: Path) -> None:
    plant_bytes(repo, "docs/notes.md", f"split{ZWSP}word\n".encode("utf-16"))
    _git(repo, "add", "-A")
    scan = scan_tree(repo)
    assert scan.findings == []
    assert set(scan.skipped) == {"docs/notes.md"}  # would fail the expected-set test above


def test_non_utf8_file_without_nul_is_reported(repo: Path) -> None:
    plant_bytes(repo, "docs/c.md", b"caf\xe9\n")
    _git(repo, "add", "-A")
    assert scan_tree(repo).findings == [
        "docs/c.md: not UTF-8 (invalid continuation byte at byte 3)"
    ]


def test_utf8_file_with_nul_is_text_and_the_nul_is_reported(repo: Path) -> None:
    plant_bytes(repo, "src/a.txt", b"one\ntwo\x00three\n")
    _git(repo, "add", "-A")
    assert scan_tree(repo) == TreeScan(findings=["src/a.txt:2: U+0000 control character"])


def test_symbolic_link_is_skipped_and_not_followed(repo: Path, tmp_path: Path) -> None:
    outside = plant(tmp_path, "outside/secret.md", f"split{ZWSP}word\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "link.md").symlink_to(outside)
    _git(repo, "add", "-A")
    assert scan_tree(repo) == TreeScan(skipped={"docs/link.md": "symbolic link, not followed"})


# --- failing loudly ------------------------------------------------------------------------------


def test_scan_fails_loudly_without_git(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_guard.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="git not found"):
        scan_tree(repo)


def test_scan_fails_loudly_outside_a_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "plain"
    plant(folder, "src/a.py", f"x = '{ZWSP}'\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never look above tmp_path
    with pytest.raises(RuntimeError, match="is not a git work tree"):
        scan_tree(folder)


def test_scan_fails_loudly_below_the_top_level(repo: Path) -> None:
    plant(repo, "src/a.py", "x = 1\n")
    with pytest.raises(RuntimeError, match="is not the top level of its git work tree"):
        scan_tree(repo / "src")


def test_files_command(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plant(repo, "src/a.py", "x = 1\n")
    plant_bytes(repo, "docs/logo.png", b"\x89PNG\x00\xff")
    _git(repo, "add", "-A")
    assert main(["files", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "skipped docs/logo.png (binary: not UTF-8 and contains NUL bytes)" in out
    assert "text guard: files: clean" in out

    plant(repo, "tests/test_a.py", f"y = '{ZWSP}'\n")
    assert main(["files", str(repo)]) == 1
    assert f"tests/test_a.py:1: {FOUND_ZWSP}" in capsys.readouterr().out

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    (tmp_path / "plain").mkdir()
    assert main(["files", str(tmp_path / "plain")]) == 2
    assert "cannot scan" in capsys.readouterr().err


# --- characters, file by file --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("\ufeff", "U+FEFF ZERO WIDTH NO-BREAK SPACE"),
        ("\u00ad", "U+00AD SOFT HYPHEN"),
        ("\u200d", "U+200D ZERO WIDTH JOINER"),
        ("\u2060", "U+2060 WORD JOINER"),
        ("\u202e", "U+202E RIGHT-TO-LEFT OVERRIDE"),
        ("\U000e0041", "U+E0041 TAG LATIN CAPITAL LETTER A"),
        ("\x00", "U+0000 control character"),
        ("\x07", "U+0007 control character"),
        ("\x1b", "U+001B control character"),
        ("\x0b", "U+000B control character"),
        ("\x7f", "U+007F control character"),
        ("\x85", "U+0085 control character"),
    ],
)
def test_format_and_control_characters_are_reported(
    tmp_path: Path, char: str, expected: str
) -> None:
    path = plant(tmp_path, "README.md", f"first\nsecond\nthird {char} line\n")
    assert findings(path, tmp_path) == [f"README.md:3: {expected}"]


def test_every_occurrence_is_reported(tmp_path: Path) -> None:
    path = plant(tmp_path, "AGENTS.md", "a\u200bb\u200bc\nok\n\u2066x\n")
    assert findings(path, tmp_path) == [
        "AGENTS.md:1: U+200B ZERO WIDTH SPACE",
        "AGENTS.md:1: U+200B ZERO WIDTH SPACE",
        "AGENTS.md:3: U+2066 LEFT-TO-RIGHT ISOLATE",
    ]


def test_tab_lf_and_cr_are_allowed(tmp_path: Path) -> None:
    path = plant(tmp_path, "docs/a.md", "| a\t| b |\r\nline\nlast\r\n")
    assert findings(path, tmp_path) == []


def test_visible_non_ascii_text_is_allowed(tmp_path: Path) -> None:
    # Umlauts, arrows, dashes, the no-break space (Zs) and an escape written as text all pass.
    path = plant(tmp_path, "docs/b.md", "Grüße → ≤ 5\u00a0% \u2013 write \\u200b as text\n")
    assert findings(path, tmp_path) == []
