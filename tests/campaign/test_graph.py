import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pecker_drum.campaign.graph import CampaignConfig, run_campaign
from pecker_drum.grading.predicates import GradingContext, KnownCanary
from pecker_drum.items.schema import Item
from pecker_drum.observation import Observation, PlantedFixture, Source
from pecker_drum.results import RetrievalSignal, RetrievalState

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
    return sorted((tmp_path / "reports" / ".checkpoints").glob("*.sqlite*"))


async def test_runs_every_attempt_and_writes_reports(item_set: Path, tmp_path: Path) -> None:
    target = FakeTarget()
    result = await run_campaign(target, config(item_set, tmp_path))
    assert sorted(target.calls) == [(f"t-00{n}", a) for n in (1, 2, 3) for a in (1, 2)]
    assert [s.successes for s in result.report.stats] == [1, 0, 0]
    assert result.report.overall.attempts == 6
    md, js = result.report_paths
    assert md.name == "2026-11-21-fake-demo_v1-fake-test.md"
    assert js.exists()
    assert "| t-001 |" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["overall"]["successes"] == 1
    assert checkpoint_files(tmp_path) == []  # finished runs leave no checkpoint behind
    again = await run_campaign(FakeTarget(), config(item_set, tmp_path))
    assert again.report_paths[0].name == "2026-11-21-fake-demo_v1-fake-test-2.md"


async def test_resume_after_crash_does_not_repeat_finished_attempts(
    item_set: Path, tmp_path: Path
) -> None:
    target = FakeTarget()
    target.crash_on_call = 4
    with pytest.raises(Crash):
        await run_campaign(target, config(item_set, tmp_path))
    finished = target.calls[:3]
    assert [p.suffix for p in checkpoint_files(tmp_path)][:1] == [".sqlite"]

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


# --- M1.5: advisory judge, run id and run record.


async def test_judge_labels_successes_only_and_never_changes_outcomes(
    item_set: Path, tmp_path: Path
) -> None:
    from pecker_drum.judge import FakeJudge
    from pecker_drum.results import JudgeLabel

    plain = await run_campaign(FakeTarget(), config(item_set, tmp_path))
    judge = FakeJudge(JudgeLabel.REPORTED)
    judged = await run_campaign(FakeTarget(), config(item_set, tmp_path, judge=judge))

    def outcomes(results: Any) -> list[tuple[str, int, str]]:
        return [(r.item_id, r.attempt, r.outcome.value) for r in results]

    assert outcomes(plain.report.results) == outcomes(judged.report.results)
    assert plain.report.stats == judged.report.stats
    assert all(r.judge is None for r in plain.report.results)
    labelled = [(r.item_id, r.attempt) for r in judged.report.results if r.judge is not None]
    assert labelled == [("t-001", 1)]  # the only success; blocked attempts get no label
    assert judged.report.info.judge == "fake:reported"
    assert judged.run_id != plain.run_id


def test_run_id_without_a_judge_is_what_it_was_before(item_set: Path) -> None:
    import hashlib

    from pecker_drum import __version__
    from pecker_drum.campaign.graph import run_id_for
    from pecker_drum.items.loader import load_item_set

    loaded = load_item_set(item_set)
    material = json.dumps(
        {
            "target": "fake",
            "settings": {"model": "fake:test"},
            "set": loaded.name,
            "digest": loaded.digest,
            "repeats": 2,
            "version": __version__,
        },
        sort_keys=True,
    )
    before = hashlib.sha256(material.encode()).hexdigest()[:16]
    assert run_id_for(FakeTarget(), loaded, 2) == before
    with_judge = run_id_for(FakeTarget(), loaded, 2, "fake:reported")
    assert with_judge != before
    assert with_judge != run_id_for(FakeTarget(), loaded, 2, "fake:complied")


# --- M2.1: the runner records the sources, the retrieval state and the signal (ADR 0010).


class SourceTarget:
    """A target that returns the sources and the planted fixtures it was built with."""

    def __init__(
        self,
        sources: tuple[Source, ...] = (),
        planted: tuple[PlantedFixture, ...] = (),
    ) -> None:
        self.sources = sources
        self.planted = planted

    @property
    def name(self) -> str:
        return "fake"

    def describe(self) -> dict[str, str]:
        return {"model": "fake:test"}

    async def run(self, item: Item, attempt: int) -> Observation:
        return Observation(answer="no leak", sources=self.sources, planted=self.planted)

    def grading_context(self, item: Item) -> GradingContext:
        return GradingContext(canaries=(KnownCanary(CANARY, "doc:x.md"),))


PLANTED = PlantedFixture(ref="doc:x.md", document_id="x.md")


async def test_an_attempt_records_the_retrieval_state_and_its_signal(
    item_set: Path, tmp_path: Path
) -> None:
    from pecker_drum.campaign.record import read_record

    target = SourceTarget(sources=(Source(id="s-1", text="[x.md] Runbook"),), planted=(PLANTED,))
    result = await run_campaign(target, config(item_set, tmp_path, repeats=1))
    states = [(r.retrieved, r.retrieval_signal) for r in result.report.results]
    assert states == [(RetrievalState.RETRIEVED, RetrievalSignal.ID)] * 3
    assert {r.source_ids for r in result.report.results} == {("s-1",)}
    record = read_record(tmp_path / "reports", result.run_id)
    assert [(r.retrieved, r.retrieval_signal) for r in record.results] == states


async def test_source_ids_keep_the_target_order_dedupe_and_escape_controls(
    item_set: Path, tmp_path: Path
) -> None:
    sources = (
        Source(id="s-1", text="[x.md] Runbook"),
        Source(id="s-bell\x07", text=""),
        Source(id="s-1", text="again"),
    )
    target = SourceTarget(sources=sources)
    result = await run_campaign(target, config(item_set, tmp_path, repeats=1))
    for attempt in result.report.results:
        assert attempt.source_ids == ("s-1", "s-bell\\u0007")
        assert (attempt.retrieved, attempt.retrieval_signal) == (None, None)  # nothing planted


async def test_a_target_that_raises_on_a_doc_item_is_unknown_not_null(tmp_path: Path) -> None:
    """The item plants a `doc:` fixture, so the state is not `null` ("the item plants nothing").

    The target raised and so reported no planted fixture; the runner falls back to the item's own
    fixtures, and the error makes the attempt `unknown` with no signal.
    """
    item_set = tmp_path / "items" / "doc_v1.jsonl"
    item_set.parent.mkdir()
    item_set.write_text(
        json.dumps(item_json("t-001") | {"fixtures": ["doc:x.md"]}) + "\n", encoding="utf-8"
    )

    class RaisingTarget(SourceTarget):
        async def run(self, item: Item, attempt: int) -> Observation:
            msg = "target exploded"
            raise ValueError(msg)

    result = await run_campaign(RaisingTarget(), config(item_set, tmp_path, repeats=1))
    attempt = result.report.results[0]
    assert attempt.error == "ValueError: target exploded"
    assert (attempt.retrieved, attempt.retrieval_signal) == (RetrievalState.UNKNOWN, None)


async def test_a_finished_run_leaves_a_run_record(item_set: Path, tmp_path: Path) -> None:
    from pecker_drum.campaign.record import read_record

    result = await run_campaign(FakeTarget(), config(item_set, tmp_path))
    record = read_record(tmp_path / "reports", result.run_id)
    assert record.info.run_id == result.run_id
    assert record.info.finished_at
    assert record.info == result.report.info
    assert record.results == result.report.results
    assert [item.id for item in record.items] == ["t-001", "t-002", "t-003"]
    assert record.missing_answers == 0
    assert {r.answer for r in record.results} == {f"answer {CANARY}", "no leak"}
    assert checkpoint_files(tmp_path) == []


# --- M2: vector filter, safety caps, lifecycle hooks.


@pytest.fixture
def mixed_set(tmp_path: Path) -> Path:
    path = tmp_path / "items" / "mixed_v1.jsonl"
    path.parent.mkdir(exist_ok=True)
    rows = [
        item_json("t-001") | {"vector": "direct"},
        item_json("t-002") | {"vector": "indirect_tool"},
        item_json("t-003") | {"vector": "indirect_doc"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


async def test_vector_filter_skips_and_lists_items(mixed_set: Path, tmp_path: Path) -> None:
    target = FakeTarget()
    vectors = frozenset({"direct", "indirect_doc"})
    result = await run_campaign(target, config(mixed_set, tmp_path, vectors=vectors))
    assert sorted({item_id for item_id, _ in target.calls}) == ["t-001", "t-003"]
    assert [s.item_id for s in result.report.stats] == ["t-001", "t-003"]
    info = result.report.info
    assert info.vectors == ("direct", "indirect_doc")
    assert [(s.id, s.vector) for s in info.skipped] == [("t-002", "indirect_tool")]
    md = result.report_paths[0].read_text(encoding="utf-8")
    assert "- Vectors: direct, indirect_doc (1 items of the set outside" in md
    assert "## Skipped (outside --vectors, 1)\n\n- t-002 (indirect_tool)" in md


def test_vector_filter_enters_the_run_id_only_when_set(mixed_set: Path) -> None:
    from pecker_drum.campaign.graph import run_id_for
    from pecker_drum.items.loader import load_item_set

    loaded = load_item_set(mixed_set)
    plain = run_id_for(FakeTarget(), loaded, 2)
    assert run_id_for(FakeTarget(), loaded, 2, vectors=None) == plain
    one = run_id_for(FakeTarget(), loaded, 2, vectors={"direct", "indirect_doc"})
    assert one != plain
    assert one == run_id_for(FakeTarget(), loaded, 2, vectors=["indirect_doc", "direct"])


async def test_consecutive_errors_abort_and_the_run_resumes(item_set: Path, tmp_path: Path) -> None:
    from pecker_drum.campaign.graph import RunAborted

    target = FakeTarget()
    target.raise_for = {"t-002", "t-003"}
    with pytest.raises(RunAborted, match="3 attempts in a row ended with an error"):
        await run_campaign(target, config(item_set, tmp_path, max_consecutive_errors=3))
    assert len(target.calls) == 5  # t-001 x2, then t-002 x2 and t-003 #1 fail in a row
    assert checkpoint_files(tmp_path)  # finished attempts stay for a resume
    target.raise_for = set()
    target.calls.clear()
    result = await run_campaign(target, config(item_set, tmp_path, max_consecutive_errors=3))
    assert result.resumed
    assert len(target.calls) == 2  # the aborted attempt is repeated, finished ones are not
    assert result.report.overall.errors == 2  # t-002 x2; the aborted t-003 #1 ran again


async def test_maximum_wall_time_aborts(item_set: Path, tmp_path: Path) -> None:
    from pecker_drum.campaign.graph import RunAborted

    target = FakeTarget()
    target.block_on_call = 2
    with pytest.raises(RunAborted, match=r"maximum wall time of 0\.2 s"):
        await run_campaign(target, config(item_set, tmp_path, max_wall_time_s=0.2))
    assert checkpoint_files(tmp_path)


class Lifecycle:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.reason: str | None = None
        self.refuse = False

    async def begin_run(self, run_id: str) -> None:
        self.events.append(f"begin {run_id}")
        if self.refuse:
            msg = "leftovers"
            raise RuntimeError(msg)

    async def end_run(self, run_id: str) -> None:
        self.events.append(f"end {run_id}")

    def abort_reason(self) -> str | None:
        return self.reason


async def test_lifecycle_brackets_every_run(item_set: Path, tmp_path: Path) -> None:
    from pecker_drum.campaign.graph import RunAborted

    hooks = Lifecycle()
    result = await run_campaign(FakeTarget(), config(item_set, tmp_path, lifecycle=hooks))
    assert hooks.events == [f"begin {result.run_id}", f"end {result.run_id}"]

    hooks = Lifecycle()
    hooks.reason = "a planted document could not be deleted"
    with pytest.raises(RunAborted, match="could not be deleted"):
        await run_campaign(FakeTarget(), config(item_set, tmp_path, lifecycle=hooks, fresh=True))
    assert [e.split()[0] for e in hooks.events] == ["begin", "end"]

    hooks = Lifecycle()
    target = FakeTarget()
    target.block_on_call = 1
    run = asyncio.create_task(
        run_campaign(target, config(item_set, tmp_path, lifecycle=hooks, fresh=True))
    )
    await asyncio.wait_for(target.blocked.wait(), timeout=10)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert [e.split()[0] for e in hooks.events] == ["begin", "end"]  # Ctrl-C still ends the run

    hooks = Lifecycle()
    hooks.refuse = True
    target = FakeTarget()
    with pytest.raises(RuntimeError, match="leftovers"):
        await run_campaign(target, config(item_set, tmp_path, lifecycle=hooks, fresh=True))
    assert target.calls == []
    assert len(hooks.events) == 1  # refused before anything ran: no end hook
