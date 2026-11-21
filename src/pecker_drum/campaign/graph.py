"""The campaign runner: a LangGraph graph with a SQLite checkpointer.

``load -> fan-out (Send per item x repeat) -> attempt (run + grade) -> aggregate -> report``.
An interrupted run resumes from the checkpoint: attempts that finished are not repeated.
"""

from __future__ import annotations

import hashlib
import json
import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.constants import END, START
from langgraph.graph.state import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Send

from pecker_drum import __version__
from pecker_drum.grading.outcome import Outcome, grade
from pecker_drum.items.loader import ItemSet, load_item_set
from pecker_drum.items.schema import Item
from pecker_drum.observation import Observation
from pecker_drum.report.render import Report, SelfTestRow, build_report, write_report
from pecker_drum.results import AttemptResult, RunInfo
from pecker_drum.targets.base import Target


class RunState(TypedDict, total=False):
    items: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]
    report_paths: list[str]


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


def run_id_for(target: Target, item_set: ItemSet, repeats: int) -> str:
    material = json.dumps(
        {
            "target": target.name,
            "settings": target.describe(),
            "set": item_set.name,
            "digest": item_set.digest,
            "repeats": repeats,
            "version": __version__,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def _attempt(target: Target, item: Item, attempt: int) -> AttemptResult:
    try:
        observation = await target.run(item, attempt)
    except Exception as exc:  # a target must not raise, but never lose the run over it
        observation = Observation(error=f"{type(exc).__name__}: {str(exc)[:300]}")
    return AttemptResult.from_grade(
        item.id, attempt, observation, grade(item, observation, target.grading_context(item))
    )


def build_graph(target: Target, config: CampaignConfig, info: RunInfo) -> Any:
    """The uncompiled graph (typed ``Any``: LangGraph's generics leak unknown types)."""

    async def load(state: RunState) -> dict[str, Any]:
        item_set = load_item_set(config.set_path)
        return {"items": [item.model_dump(mode="json") for item in item_set.items]}

    def fan_out(state: RunState) -> list[Send]:
        return [
            Send("attempt", AttemptInput(item=item, attempt=attempt))
            for item in state.get("items", [])
            for attempt in range(1, config.repeats + 1)
        ]

    async def attempt(payload: AttemptInput) -> dict[str, Any]:
        item = Item.model_validate(payload["item"])
        result = await _attempt(target, item, payload["attempt"])
        return {"results": [result.model_dump(mode="json")]}

    async def report(state: RunState) -> dict[str, Any]:
        items = tuple(Item.model_validate(raw) for raw in state.get("items", []))
        results = tuple(AttemptResult.model_validate(raw) for raw in state.get("results", []))
        rows = config.self_test(items, results) if config.self_test else []
        finished = info.model_copy(update={"finished_at": _now()})
        built = build_report(finished, items, results, rows)
        md, js = write_report(built, config.reports_dir, config.today or date.today())
        return {"report_paths": [str(md), str(js)]}

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
    run_id = run_id_for(target, item_set, config.repeats)
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
        started_at=_now(),
        harness_version=__version__,
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
            final = await graph.ainvoke(None, cast("Any", run_config), durability="sync")
        else:
            final = await graph.ainvoke({}, cast("Any", run_config), durability="sync")
    state = cast("dict[str, Any]", final)
    items = tuple(Item.model_validate(raw) for raw in state.get("items", []))
    results = tuple(AttemptResult.model_validate(raw) for raw in state.get("results", []))
    rows = config.self_test(items, results) if config.self_test else []
    report = build_report(info, items, results, rows)
    md, js = (Path(path) for path in state["report_paths"])
    _remove_checkpoint(db_path)  # finished: the report is the durable record
    return CampaignResult(run_id, report, (md, js), resumed, already_done)


def outcome_counts(results: tuple[AttemptResult, ...]) -> dict[Outcome, int]:
    counts = dict.fromkeys(Outcome, 0)
    for result in results:
        counts[result.outcome] += 1
    return counts
