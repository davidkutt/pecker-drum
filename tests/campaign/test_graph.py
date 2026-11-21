import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pecker_drum.campaign.graph import CampaignConfig, run_campaign
from pecker_drum.grading.predicates import GradingContext, KnownCanary
from pecker_drum.items.schema import Item
from pecker_drum.observation import Observation

CANARY = "CANARY-00000000ee01"


def item_json(item_id: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": f"item {item_id}",
        "vector": "direct",
        "lang": "en",
        "severity": "high",
        "owasp": ["LLM01:2026"],
        "user": {"sub": "carol"},
        "turns": ["hi"],
        "succeeds_if": [{"kind": "canary_in_answer"}],
        "origin": "hand",
        "self_test": {"answer": CANARY},
    }


@pytest.fixture
def item_set(tmp_path: Path) -> Path:
    path = tmp_path / "items" / "demo_v1.jsonl"
    path.parent.mkdir()
    lines = [json.dumps(item_json(f"t-00{n}")) for n in (1, 2, 3)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Crash(BaseException):
    """Simulates the process dying mid-run (not an ordinary target error)."""


class FakeTarget:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.crash_on_call: int | None = None
        self.block_on_call: int | None = None
        self.blocked = asyncio.Event()
        self.raise_for: set[str] = set()

    @property
    def name(self) -> str:
        return "fake"

    def describe(self) -> dict[str, str]:
        return {"model": "fake:test"}

    async def run(self, item: Item, attempt: int) -> Observation:
        self.calls.append((item.id, attempt))
        if self.crash_on_call == len(self.calls):
            raise Crash
        if self.block_on_call == len(self.calls):
            self.blocked.set()
            await asyncio.Event().wait()  # never returns; the test cancels the run
        if item.id in self.raise_for:
            msg = "target exploded"
            raise ValueError(msg)
        leaked = item.id == "t-001" and attempt == 1
        return Observation(answer=f"answer {CANARY}" if leaked else "no leak")

    def grading_context(self, item: Item) -> GradingContext:
        return GradingContext(canaries=(KnownCanary(CANARY, "doc:x.md"),))


def config(item_set: Path, tmp_path: Path, **overrides: Any) -> CampaignConfig:
    values: dict[str, Any] = {
        "set_path": item_set,
        "repeats": 2,
        "concurrency": 1,
        "reports_dir": tmp_path / "reports",
        "today": date(2026, 11, 21),
    }
    values.update(overrides)
    return CampaignConfig(**values)


def checkpoint_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "reports" / ".checkpoints").glob("*.sqlite"))


async def test_runs_every_attempt_and_writes_reports(item_set: Path, tmp_path: Path) -> None:
    target = FakeTarget()
    result = await run_campaign(target, config(item_set, tmp_path))
    assert sorted(target.calls) == [(f"t-00{n}", a) for n in (1, 2, 3) for a in (1, 2)]
    assert [s.successes for s in result.report.stats] == [1, 0, 0]
    assert result.report.overall.attempts == 6
    md, js = result.report_paths
    assert md.name == "2026-11-21-fake-demo_v1.md"
    assert js.exists()
    assert "| t-001 |" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["overall"]["successes"] == 1
    assert checkpoint_files(tmp_path) == []  # finished runs leave no checkpoint behind
    again = await run_campaign(FakeTarget(), config(item_set, tmp_path))
    assert again.report_paths[0].name == "2026-11-21-fake-demo_v1-2.md"


async def test_resume_after_crash_does_not_repeat_finished_attempts(
    item_set: Path, tmp_path: Path
) -> None:
    target = FakeTarget()
    target.crash_on_call = 4
    with pytest.raises(Crash):
        await run_campaign(target, config(item_set, tmp_path))
    finished = target.calls[:3]
    assert len(checkpoint_files(tmp_path)) == 1

    target.crash_on_call = None
    target.calls.clear()
    result = await run_campaign(target, config(item_set, tmp_path))
    assert result.resumed
    assert result.already_done == 3
    assert not set(finished) & set(target.calls)
    assert len(target.calls) == 3
    assert result.report.overall.attempts == 6
    assert sorted((r.item_id, r.attempt) for r in result.report.results) == sorted(
        [(f"t-00{n}", a) for n in (1, 2, 3) for a in (1, 2)]
    )


async def test_resume_after_cancellation(item_set: Path, tmp_path: Path) -> None:
    """Ctrl-C cancels the running task; finished attempts must survive in the checkpoint."""
    target = FakeTarget()
    target.block_on_call = 4
    run = asyncio.create_task(run_campaign(target, config(item_set, tmp_path)))
    await asyncio.wait_for(target.blocked.wait(), timeout=10)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    finished = target.calls[:3]

    target.block_on_call = None
    target.calls.clear()
    result = await run_campaign(target, config(item_set, tmp_path))
    assert result.resumed
    assert not set(finished) & set(target.calls)
    assert result.report.overall.attempts == 6


async def test_fresh_ignores_an_unfinished_checkpoint(item_set: Path, tmp_path: Path) -> None:
    target = FakeTarget()
    target.crash_on_call = 2
    with pytest.raises(Crash):
        await run_campaign(target, config(item_set, tmp_path))
    target.crash_on_call = None
    target.calls.clear()
    result = await run_campaign(target, config(item_set, tmp_path, fresh=True))
    assert not result.resumed
    assert len(target.calls) == 6


async def test_target_exceptions_become_error_results(item_set: Path, tmp_path: Path) -> None:
    target = FakeTarget()
    target.raise_for = {"t-002"}
    result = await run_campaign(target, config(item_set, tmp_path, repeats=1))
    errors = [r for r in result.report.results if r.outcome == "error"]
    assert [(r.item_id, r.error) for r in errors] == [("t-002", "ValueError: target exploded")]
    assert result.report.overall.errors == 1
    assert result.report.overall.attempts == 2  # errors are excluded from rates
