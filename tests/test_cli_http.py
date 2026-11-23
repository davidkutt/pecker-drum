"""The CLI with HTTP targets (M2): ``--target stub``, private targets, ``drum cleanup``.

Every server here is the stand-in on 127.0.0.1; nothing else is reachable.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest

from pecker_drum.cli import EXIT_INTERRUPTED, EXIT_OK, EXIT_STOPPED, EXIT_USAGE, main
from pecker_drum.items.loader import load_item_set
from pecker_drum.netguard import block_network
from pecker_drum.targets.stub import StubMode, StubOptions, StubServer

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "docs" / "examples" / "http-target.toml"
ITEMS = REPO / "items"
FIXTURES = REPO / "fixtures"
SCOPE = "direct,indirect_doc"
IN_SCOPE = ["pub-009", "pub-010", "pub-011", "pub-012", "pub-013"]


def run_cli(*args: str | Path) -> int:
    return main([str(arg) for arg in args])


def json_report(folder: Path) -> dict[str, Any]:
    (path,) = sorted(folder.glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def outcomes(report: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for attempt in report["attempts"]:
        found.setdefault(attempt["item_id"], []).append(attempt["outcome"])
    return found


# --- the stand-in versus the twin -------------------------------------------------------------


@pytest.mark.parametrize("model", ["fake:obedient", "fake:safe"])
@pytest.mark.parametrize("judge", [None, "fake:reported"])
def test_stub_self_test_matches_the_twin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model: str, judge: str | None
) -> None:
    extra = ["--judge", judge] if judge else []
    common = ["--set", "public_v1", "--model", model, "--vectors", SCOPE, "--items-dir", ITEMS]
    common += ["--fixtures-dir", FIXTURES, *extra]
    twin_dir, stub_dir = tmp_path / "twin", tmp_path / "stub"
    assert run_cli("run", "--target", "twin", *common, "--reports-dir", twin_dir) == EXIT_OK
    assert (
        run_cli(
            "run",
            "--target",
            "stub",
            "--target-config",
            EXAMPLE,
            *common,
            "--reports-dir",
            stub_dir,
        )
        == EXIT_OK
    )
    assert capsys.readouterr().out.count("self-test passed") == 2
    twin, stub = json_report(twin_dir), json_report(stub_dir)
    assert outcomes(stub) == outcomes(twin)
    assert sorted(outcomes(stub)) == IN_SCOPE
    expected = "success" if model == "fake:obedient" else "blocked"
    assert outcomes(stub)["pub-010"] == [expected]
    assert [row["ok"] for row in stub["self_test"]] == [True] * 5
    assert len(stub["run"]["skipped"]) == 10
    assert stub["run"]["vectors"] == ["direct", "indirect_doc"]
    assert (
        stub["run"]["settings"]["observed"]
        == "answer, sources, tool_calls, tool_results, approvals"
    )
    labels = [a["judge"] for a in stub["attempts"] if a["outcome"] in ("success", "contained")]
    if judge:
        assert bool(labels) is (model == "fake:obedient")  # safe runs have nothing to label
        assert all(label["label"] == "reported" for label in labels)
    else:
        assert all(label is None for label in labels)
    (md,) = stub_dir.glob("*.md")
    slug = model.replace(":", "-")
    assert md.name.endswith(f"-stub-public_v1-example-api-{slug}.md")


def test_stub_run_without_reports_dir_writes_under_private_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    args = ["--target-config", EXAMPLE, "--set", "public_v1", "--model", "fake:obedient"]
    args += ["--vectors", "direct", "--items-dir", ITEMS, "--fixtures-dir", FIXTURES]
    assert run_cli("run", "--target", "stub", *args) == EXIT_OK
    private = tmp_path / "private" / "reports"
    report = json_report(private)
    run_id = report["run"]["run_id"]
    assert (private / ".runs" / f"{run_id}.json").is_file()
    assert len(list(private.glob("*.md"))) == 1
    assert not (tmp_path / "reports").exists()
    capsys.readouterr()

    assert run_cli("report", "--run-id", run_id) == EXIT_OK  # found in private/reports by default
    assert f"re-rendered from private/reports/.runs/{run_id}.json" in capsys.readouterr().out
    assert len(list(private.glob("*.md"))) == 2
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--target", "stub", "--model", "fake:complied"], "--target stub takes --model"),
        (["--target", "stub", "--model", "target"], "--target stub takes --model"),
        (["--target", "stub", "--model", "openrouter:vendor/x"], "--target stub takes --model"),
        (["--target", "lab", "--model", "fake:safe"], "use --model target"),
        (["--target", "lab", "--model", "openrouter"], "use --model target"),
        (["--target", "missing", "--model", "target"], "unknown target 'missing'"),
    ],
)
def test_http_target_usage_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "private" / "targets").mkdir(parents=True)
    (tmp_path / "private" / "targets" / "lab.toml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "docs" / "examples").mkdir(parents=True)
    (tmp_path / "docs" / "examples" / "http-target.toml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-test-should-never-load\n", encoding="utf-8")
    monkeypatch.setattr("pecker_drum.cli.ENV_FILE", env)
    common = ["--set", "public_v1", "--items-dir", ITEMS, "--fixtures-dir", FIXTURES]
    assert run_cli("run", *args, *common) == EXIT_USAGE
    assert message in capsys.readouterr().err
    assert "OPENROUTER_API_KEY" not in os.environ  # nothing opened .env


# --- a private HTTP target: the stand-in started by the test, a config naming it ----------------


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Callable[..., Path]]:
    """``make(options, **replacements) -> config path`` for a stand-in run as a private target."""
    block_network(allow_loopback=True)
    servers: list[StubServer] = []
    items = load_item_set(ITEMS / "public_v1.jsonl").items

    def make(
        options: StubOptions | None = None,
        mode: StubMode = StubMode.OBEDIENT,
        replacements: dict[str, str] | None = None,
    ) -> Path:
        passwords = {sub: secrets.token_urlsafe(18) for sub in ("carol", "dana", "erin")}
        for sub, password in passwords.items():
            monkeypatch.setenv(f"DRUM_LAB_PASSWORD_{sub.upper()}", password)
        server = StubServer(items, mode, passwords, options).start()
        servers.append(server)
        text = EXAMPLE.read_text(encoding="utf-8")
        text = text.replace('base_url = "http://127.0.0.1:8080"', f'base_url = "{server.url}"')
        text = text.replace('label = "example-api"', 'label = "lab-app"')
        text = text.replace("DRUM_EXAMPLE_PASSWORD_", "DRUM_LAB_PASSWORD_")
        for old, new in (replacements or {}).items():
            assert old in text, old
            text = text.replace(old, new)
        path = tmp_path / f"lab-{len(servers)}.toml"
        path.write_text(text, encoding="utf-8")
        make.server = server  # type: ignore[attr-defined]
        make.passwords = passwords  # type: ignore[attr-defined]
        return path

    yield make
    for server in servers:
        server.stop()


def lab_run(config: Path, reports: Path, *extra: str) -> int:
    args = ["run", "--target", "lab", "--target-config", config, "--model", "target"]
    args += ["--set", "public_v1", "--items-dir", ITEMS, "--fixtures-dir", FIXTURES]
    return run_cli(*args, "--reports-dir", reports, *extra)


def test_private_target_run_reports_under_its_label(
    lab: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = lab()
    assert lab_run(config, tmp_path / "out", "--vectors", SCOPE, "--concurrency", "4") == EXIT_OK
    out = capsys.readouterr().out
    assert "concurrency 1: this target plants documents" in out
    assert "self-test" not in out  # the target's own model: nothing to compare against
    report = json_report(tmp_path / "out")
    assert report["run"]["model"] == "target"
    assert report["run"]["settings"]["label"] == "lab-app"
    assert outcomes(report)["pub-013"] == ["contained"]
    (md,) = (tmp_path / "out").glob("*.md")
    assert md.name.endswith("-lab-public_v1-lab-app.md")
    assert lab.server.state.documents == {}  # type: ignore[attr-defined]


def test_preflight_refuses_items_the_target_cannot_run(
    lab: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = lab()
    assert lab_run(config, tmp_path / "out") == EXIT_USAGE  # no --vectors: scenario items too
    err = capsys.readouterr().err
    assert "pub-001: scenario:issue-hidden-comment is not supported" in err
    assert "narrow the set with --vectors" in err
    assert lab.server.state.requests == []  # type: ignore[attr-defined]


def test_leftovers_fail_loudly_block_the_next_run_and_cleanup_removes_them(
    lab: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = lab(StubOptions(ignore_deletes=True))
    server: StubServer = lab.server  # type: ignore[attr-defined]
    assert lab_run(config, tmp_path / "out", "--vectors", "indirect_doc") == EXIT_STOPPED
    err = capsys.readouterr().err
    run_id = json_report(tmp_path / "out")["run"]["run_id"]
    assert f"ERROR: 3 planted documents of run {run_id} are still on the target" in err
    assert f"drum-fixture-{run_id}-runbook-cache-restart.md" in err
    command = f"uv run drum cleanup --target lab --target-config {config} --run-id {run_id}"
    assert command in err

    assert lab_run(config, tmp_path / "again", "--vectors", "direct", "--fresh") == EXIT_STOPPED
    err = capsys.readouterr().err
    assert "3 planted documents are on the target before the run" in err
    assert "nothing was run" in err
    assert command in err
    assert not (tmp_path / "again").exists()

    server.options.ignore_deletes = False
    assert run_cli("cleanup", "--target", "lab", "--target-config", config, "--run-id", run_id) == (
        EXIT_OK
    )
    assert f"run {run_id}: 3 planted documents found, 3 deleted, 0 left" in capsys.readouterr().out
    assert server.state.documents == {}
    assert lab_run(config, tmp_path / "third", "--vectors", "direct") == EXIT_OK


def test_cleanup_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("cleanup", "--target", "stub", "--run-id", "0123456789abcdef") == EXIT_OK
    assert "keeps nothing between runs" in capsys.readouterr().out
    assert run_cli("cleanup", "--target", "x", "--run-id", "../../etc") == EXIT_USAGE
    assert run_cli("cleanup", "--target", "../x", "--run-id", "0123456789abcdef") == EXIT_USAGE


def test_max_attempts_refuses_before_anything_runs(
    lab: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = lab(replacements={"max_attempts = 1000": "max_attempts = 9"})
    assert lab_run(config, tmp_path / "out", "--vectors", SCOPE, "--repeats", "2") == EXIT_USAGE
    assert "10 attempts (5 items x 2 repeats) exceed limits.max_attempts = 9" in (
        capsys.readouterr().err
    )
    assert lab.server.state.requests == []  # type: ignore[attr-defined]


def test_errors_in_a_row_stop_the_run_and_record_the_status(
    lab: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = lab(
        StubOptions(message_status=429),
        replacements={"max_consecutive_errors = 5": "max_consecutive_errors = 2"},
    )
    assert lab_run(config, tmp_path / "out", "--vectors", SCOPE) == EXIT_STOPPED
    err = capsys.readouterr().err
    assert "stopped: 2 attempts in a row ended with an error" in err
    assert "send message: HTTP 429" in err
    assert "resume from the checkpoint" in err
    assert lab.server.state.documents == {}  # type: ignore[attr-defined]
    assert list((tmp_path / "out" / ".checkpoints").glob("*.sqlite"))


def test_ctrl_c_cleans_up_planted_documents(
    lab: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt() -> None:  # in the stand-in's thread, while the answer is being streamed
        os.kill(os.getpid(), signal.SIGINT)

    config = lab(StubOptions(stall=3.0, on_stream=interrupt))
    assert lab_run(config, tmp_path / "out", "--vectors", "indirect_doc") == EXIT_INTERRUPTED
    err = capsys.readouterr().err
    assert "interrupted: planted documents were cleaned up" in err
    server: StubServer = lab.server  # type: ignore[attr-defined]
    assert server.state.deleted  # the attempt's finally and the run's sweep removed it
    assert server.state.documents == {}
    assert server.state.conversations == {}


# --- secrets never leak --------------------------------------------------------------------------


def _pieces(secret: str) -> set[str]:
    return {secret[i : i + 8] for i in range(len(secret) - 7)}


def test_passwords_and_tokens_never_leak(
    lab: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A leaky stand-in echoes the bearer token and the password in answers, sources, tool
    arguments and error bodies. None of it may reach a log record, stdout, stderr, a report, a
    run record, a checkpoint or a run id, not even as an 8-character piece."""
    caplog.set_level(logging.DEBUG)
    out = tmp_path / "out"
    config = lab(StubOptions(leaky=True))
    assert lab_run(config, out, "--vectors", SCOPE, "--judge", "fake:reported") == EXIT_OK
    first: StubServer = lab.server  # type: ignore[attr-defined]
    leaked = [*lab.passwords.values(), *first.state.issued]  # type: ignore[attr-defined]
    run_id = json_report(out)["run"]["run_id"]
    report_text = next(out.glob("*.json")).read_text(encoding="utf-8")
    assert "[redacted]" in report_text  # the leak paths were exercised and redacted

    # An aborted run keeps its checkpoint: the leaky error body goes in there.
    aborted = lab(
        StubOptions(leaky=True, message_status=500),
        replacements={"max_consecutive_errors = 5": "max_consecutive_errors = 1"},
    )
    assert lab_run(aborted, out, "--vectors", SCOPE, "--fresh") == EXIT_STOPPED
    second: StubServer = lab.server  # type: ignore[attr-defined]
    leaked += [*lab.passwords.values(), *second.state.issued]  # type: ignore[attr-defined]
    assert list((out / ".checkpoints").glob("*.sqlite"))

    captured = capsys.readouterr()
    texts = [captured.out, captured.err]
    texts += [record.getMessage() for record in caplog.records]
    texts += [run_id]
    for path in out.rglob("*"):
        if path.is_file():
            texts.append(path.read_bytes().decode("latin-1"))
    haystack = "\n".join(texts)
    for secret in leaked:
        for piece in _pieces(secret):
            assert piece not in haystack, "a piece of a password or token leaked"


def test_run_id_does_not_depend_on_password_values(
    lab: Callable[..., Path], tmp_path: Path
) -> None:
    config = lab()
    assert lab_run(config, tmp_path / "a", "--vectors", "direct") == EXIT_OK
    for sub in ("carol", "dana", "erin"):
        os.environ[f"DRUM_LAB_PASSWORD_{sub.upper()}"] = secrets.token_urlsafe(18)
    lab.server.state.users.update(  # type: ignore[attr-defined]
        {sub: os.environ[f"DRUM_LAB_PASSWORD_{sub.upper()}"] for sub in ("carol", "dana", "erin")}
    )
    assert lab_run(config, tmp_path / "b", "--vectors", "direct") == EXIT_OK
    first = json_report(tmp_path / "a")["run"]["run_id"]
    assert json_report(tmp_path / "b")["run"]["run_id"] == first


def test_report_searches_reports_then_private_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_cli("report", "--run-id", "0123456789abcdef") == EXIT_USAGE
    err = capsys.readouterr().err
    assert "no finished run 0123456789abcdef in reports/.runs or private/reports/.runs" in err
