"""Both harness self-tests over the real public_v1 set (no network, as in CI)."""

from pathlib import Path

import pytest

from pecker_drum.cli import EXIT_OK, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("model", ["fake:obedient", "fake:safe"])
def test_public_v1_self_test(
    model: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
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
