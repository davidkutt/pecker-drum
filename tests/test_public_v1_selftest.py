"""Both harness self-tests over the real public_v1 set (no network, as in CI)."""

import re
from pathlib import Path

import pytest

from pecker_drum.cli import EXIT_OK, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("repeats", [1, 3])
@pytest.mark.parametrize("model", ["fake:obedient", "fake:safe"])
def test_public_v1_self_test(
    model: str, repeats: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "run",
            "--target",
            "twin",
            "--set",
            "public_v1",
            "--model",
            model,
            "--repeats",
            str(repeats),
            "--items-dir",
            str(ROOT / "items"),
            "--fixtures-dir",
            str(ROOT / "fixtures"),
            "--reports-dir",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "self-test passed" in out
    (report,) = tmp_path.glob("*.md")
    text = report.read_text(encoding="utf-8")
    assert not COLLAPSED.search(text), COLLAPSED.findall(text)
    rows = category_rows(text)
    assert rows
    if model == "fake:safe":
        n = 15 * repeats
        bound = {1: "18.2", 3: "6.5"}[repeats]
        assert f"- **ASR@1:** 0/{n}, ≤ {bound} % (0/{n} attempts" in text
        assert all(row.count("≤") == (2 if repeats > 1 else 1) for row in rows), rows


COLLAPSED = re.compile(r"\[95 % CI (\d+\.\d) to \1 %")
"""An interval whose two ends render the same, for example ``[95 % CI 0.0 to 0.0 %]``."""


def category_rows(text: str) -> list[str]:
    section = text[text.index("## Per OWASP category") : text.index("## Cost and latency")]
    return [line for line in section.splitlines() if line.startswith("| LLM")]
