"""The campaign runner: a LangGraph graph with a SQLite checkpointer.

``load -> fan-out (Send per item x repeat) -> attempt (run + grade) -> aggregate -> report``.
An interrupted run resumes from the checkpoint: attempts that finished are not repeated.

Safety caps (M2): a maximum wall time and a maximum number of attempts in a row that end with an
error stop the run with ``RunAborted``; the checkpoint keeps every finished attempt, so the same
command resumes. A ``RunLifecycle`` (the HTTP target) is told when a run begins and ends, also
when it is interrupted, and can stop the run between attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import operator
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol, TypedDict, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.constants import END, START
from langgraph.graph.state import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Send

from pecker_drum import __version__
from pecker_drum.campaign.record import RunRecord, write_record
from pecker_drum.grading.outcome import Outcome, grade
from pecker_drum.grading.retrieval import retrieval as match_retrieval
from pecker_drum.items.loader import ItemSet, load_item_set
from pecker_drum.items.schema import Item
from pecker_drum.judge import Judge, judge_result
from pecker_drum.observation import Observation, PlantedFixture
from pecker_drum.report.render import Report, SelfTestRow, build_report, write_report
from pecker_drum.results import AttemptResult, RunInfo, SkippedItem
from pecker_drum.targets.base import Target


class RunAborted(RuntimeError):
    """A safety cap or the target stopped the run. Finished attempts stay in the checkpoint."""


class RunLifecycle(Protocol):
    """Hooks for targets that keep state outside the harness (planted documents, M2)."""

    async def begin_run(self, run_id: str) -> None:
        """Before the first attempt. May raise to refuse the run."""
        ...

    async def end_run(self, run_id: str) -> None:
        """After the last attempt, also after an error, an abort or Ctrl-C. Must not raise."""
        ...

    def abort_reason(self) -> str | None:
        """Checked after every attempt: a reason stops the run (``RunAborted``)."""
        ...


class RunState(TypedDict, total=False):
    items: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]
    report_paths: list[str]
    info: dict[str, Any]
    """The run info as written to the report (with ``finished_at``)."""


class AttemptInput(TypedDict):
    item: dict[str, Any]
    attempt: int


SelfTestCheck = Callable[[tuple[Item, ...], tuple[AttemptResult, ...]], list[SelfTestRow]]


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    set_path: Path
    repeats: int = 1
    concurrency: int = 4
    reports_dir: Path = Path("reports")
    checkpoint_dir: Path | None = None
    """Default: ``<reports_dir>/.checkpoints``."""
    fresh: bool = False
    """Ignore an unfinished checkpoint and start over."""
    today: date | None = None
    self_test: SelfTestCheck | None = None
    judge: Judge | None = None
    """Advisory judge for success and contained attempts (ADR 0008); never changes an outcome."""
    vectors: frozenset[str] | None = None
    """Run only items with these vectors (``--vectors``); ``None`` runs every item."""
    max_wall_time_s: float | None = None
    """Stop the run (``RunAborted``) once it has run this long."""
    max_consecutive_errors: int | None = None
    """Stop the run once this many attempts in a row ended with an error."""
    lifecycle: RunLifecycle | None = None

    def checkpoints(self) -> Path:
        return self.checkpoint_dir or self.reports_dir / ".checkpoints"


@dataclass(slots=True)
class CampaignResult:
    run_id: str
    report: Report
    report_paths: tuple[Path, Path]
    resumed: bool = False
    already_done: int = 0
    """Attempts that finished before the interruption (not run again)."""
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


def _remove_checkpoint(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def in_scope(items: Sequence[Item], vectors: Collection[str] | None) -> tuple[Item, ...]:
    """The items a ``--vectors`` filter keeps (all of them without a filter)."""
    return tuple(items) if vectors is None else tuple(i for i in items if i.vector in vectors)


def skipped_items(
    items: Sequence[Item], vectors: Collection[str] | None
) -> tuple[SkippedItem, ...]:
    if vectors is None:
        return ()
    return tuple(
        SkippedItem(id=i.id, vector=i.vector.value) for i in items if i.vector not in vectors
    )


def run_id_for(
    target: Target,
    item_set: ItemSet,
    repeats: int,
    judge: str = "",
    vectors: Collection[str] | None = None,
) -> str:
    """A hash of everything that shapes the attempts. The judge and the vector filter count only
    when set, so run ids without them stay what they were; with a judge, a resume keeps its
    labels."""
    fields: dict[str, Any] = {
        "target": target.name,
        "settings": target.describe(),
        "set": item_set.name,
        "digest": item_set.digest,
        "repeats": repeats,
        "version": __version__,
    }
    if judge:
        fields["judge"] = judge
    if vectors is not None:
        fields["vectors"] = sorted(vectors)
    material = json.dumps(fields, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def _attempt(
    target: Target, item: Item, attempt: int, judge: Judge | None = None
) -> AttemptResult:
    try:
        observation = await target.run(item, attempt)
    except Exception as exc:  # a target must not raise, but never lose the run over it
        observation = Observation(error=f"{type(exc).__name__}: {str(exc)[:300]}")
    context = target.grading_context(item)
    # A target that raised reported nothing it planted, but the item's fixtures still went in (or
    # would have): fall back to them by ref, so `null` keeps meaning "the item plants nothing".
    planted = observation.planted or tuple(PlantedFixture(ref=ref) for ref in item.fixtures)
    result = AttemptResult.from_grade(
        item.id,
        attempt,
        observation,
        grade(item, observation, context),
        match_retrieval(observation, planted, context.canaries),
    )
    if judge is not None:  # after grading: the label can never feed back into the outcome
        result = await judge_result(judge, item, result)
    return result


def build_graph(target: Target, config: CampaignConfig, info: RunInfo) -> Any:
    """The uncompiled graph (typed ``Any``: LangGraph's generics leak unknown types)."""
    streak = {"errors": 0}
    stopped: dict[str, str] = {}
    """The first abort reason; later attempts see it before they reach the target."""

    async def load(state: RunState) -> dict[str, Any]:
        item_set = load_item_set(config.set_path)
        items = in_scope(item_set.items, config.vectors)
        return {"items": [item.model_dump(mode="json") for item in items]}

    def fan_out(state: RunState) -> list[Send]:
        return [
            Send("attempt", AttemptInput(item=item, attempt=attempt))
            for item in state.get("items", [])
            for attempt in range(1, config.repeats + 1)
        ]

    def check_caps(result: AttemptResult) -> None:
        """Raise ``RunAborted`` after this attempt if a cap is reached. The attempt raising is not
        saved, so a resumed run repeats it."""
        limit = config.max_consecutive_errors
        streak["errors"] = streak["errors"] + 1 if result.error is not None else 0
        if limit is not None and streak["errors"] >= limit:
            stopped.setdefault(
                "reason",
                f"{limit} attempts in a row ended with an error (last: {result.item_id} "
                f"#{result.attempt}: {result.error})",
            )
        reason = config.lifecycle.abort_reason() if config.lifecycle else None
        if reason:
            stopped.setdefault("reason", reason)
        if stopped:
            raise RunAborted(stopped["reason"])

    async def attempt(payload: AttemptInput) -> dict[str, Any]:
        if stopped:  # LangGraph may start a queued attempt before the abort propagates
            raise RunAborted(stopped["reason"])
        item = Item.model_validate(payload["item"])
        result = await _attempt(target, item, payload["attempt"], config.judge)
        check_caps(result)
        return {"results": [result.model_dump(mode="json")]}

    async def report(state: RunState) -> dict[str, Any]:
        items = tuple(Item.model_validate(raw) for raw in state.get("items", []))
        results = tuple(AttemptResult.model_validate(raw) for raw in state.get("results", []))
        rows = config.self_test(items, results) if config.self_test else []
        finished_at = utc_now()
        finished = info.model_copy(update={"finished_at": finished_at})
        built = build_report(finished, items, results, rows, generated_at=finished_at)
        md, js = write_report(built, config.reports_dir, config.today or date.today())
        record = RunRecord(info=finished, items=items, results=built.results, self_test=tuple(rows))
        write_record(record, config.reports_dir)
        return {"report_paths": [str(md), str(js)], "info": finished.model_dump(mode="json")}

    builder: Any = StateGraph(RunState)
    builder.add_node("load", load)
    builder.add_node("attempt", attempt)
    builder.add_node("report", report)
    builder.add_edge(START, "load")
    builder.add_conditional_edges("load", fan_out, ["attempt"])
    builder.add_edge("attempt", "report")
    builder.add_edge("report", END)
    return builder


async def run_campaign(target: Target, config: CampaignConfig) -> CampaignResult:
    item_set = load_item_set(config.set_path)
    judge_name = config.judge.name if config.judge else ""
    run_id = run_id_for(target, item_set, config.repeats, judge_name, config.vectors)
    if config.lifecycle is None:
        return await _run_graph(target, config, item_set, run_id, judge_name)
    await config.lifecycle.begin_run(run_id)
    try:
        return await _run_graph(target, config, item_set, run_id, judge_name)
    finally:
        await config.lifecycle.end_run(run_id)


async def _invoke(graph: Any, payload: Any, run_config: Any, limit_s: float | None) -> Any:
    if limit_s is None:
        return await graph.ainvoke(payload, run_config, durability="sync")
    deadline = asyncio.timeout(limit_s)
    try:
        async with deadline:
            return await graph.ainvoke(payload, run_config, durability="sync")
    except TimeoutError:
        if deadline.expired():
            msg = f"the run reached its maximum wall time of {limit_s:g} s"
            raise RunAborted(msg) from None
        raise


async def _run_graph(
    target: Target, config: CampaignConfig, item_set: ItemSet, run_id: str, judge_name: str
) -> CampaignResult:
    checkpoints = config.checkpoints()
    checkpoints.mkdir(parents=True, exist_ok=True)
    db_path = checkpoints / f"{run_id}.sqlite"
    if config.fresh:
        _remove_checkpoint(db_path)
    info = RunInfo(
        run_id=run_id,
        target=target.name,
        item_set=item_set.name,
        set_digest=item_set.digest,
        model=target.describe().get("model", ""),
        repeats=config.repeats,
        settings=target.describe(),
        started_at=utc_now(),
        harness_version=__version__,
        judge=judge_name,
        vectors=tuple(sorted(config.vectors)) if config.vectors is not None else (),
        skipped=skipped_items(item_set.items, config.vectors),
    )
    run_config: dict[str, Any] = {
        "configurable": {"thread_id": run_id},
        "max_concurrency": config.concurrency,
        "recursion_limit": 25,
    }
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph: Any = build_graph(target, config, info).compile(checkpointer=saver)
        snapshot = await graph.aget_state(cast("Any", run_config))
        resumed = bool(snapshot.next)
        already_done = 0
        if resumed:
            # The snapshot's values already include the writes of finished attempts.
            values = cast("dict[str, Any]", snapshot.values or {})
            already_done = len(values.get("results", []))
            final = await _invoke(graph, None, run_config, config.max_wall_time_s)
        else:
            final = await _invoke(graph, {}, run_config, config.max_wall_time_s)
    state = cast("dict[str, Any]", final)
    items = tuple(Item.model_validate(raw) for raw in state.get("items", []))
    results = tuple(AttemptResult.model_validate(raw) for raw in state.get("results", []))
    rows = config.self_test(items, results) if config.self_test else []
    finished = RunInfo.model_validate(state["info"]) if "info" in state else info
    report = build_report(finished, items, results, rows, generated_at=finished.finished_at)
    md, js = (Path(path) for path in state["report_paths"])
    _remove_checkpoint(db_path)  # finished: the report is the durable record
    return CampaignResult(run_id, report, (md, js), resumed, already_done)


def outcome_counts(results: tuple[AttemptResult, ...]) -> dict[Outcome, int]:
    counts = dict.fromkeys(Outcome, 0)
    for result in results:
        counts[result.outcome] += 1
    return counts
