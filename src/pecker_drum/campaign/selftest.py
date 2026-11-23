"""Harness self-test: fake models must produce exactly the outcomes their items predict."""

from __future__ import annotations

from collections.abc import Callable

from pecker_drum.grading.outcome import (
    expected_obedient_outcome,
    expected_safe_outcome,
    reachable_kinds,
)
from pecker_drum.grading.predicates import GradingContext
from pecker_drum.items.schema import Item
from pecker_drum.report.render import SelfTestRow
from pecker_drum.results import AttemptResult
from pecker_drum.twin.models import ModelSpec


def self_test_check(
    spec: ModelSpec, context_for: Callable[[Item], GradingContext]
) -> Callable[[tuple[Item, ...], tuple[AttemptResult, ...]], list[SelfTestRow]]:
    """``fake:obedient``: every reachable predicate fires and the outcome follows from them.
    ``fake:safe``: nothing fires, every attempt is ``blocked``. Items whose predicates need a
    channel the target does not expose expect ``unobservable`` when nothing reachable fires."""

    def check(items: tuple[Item, ...], results: tuple[AttemptResult, ...]) -> list[SelfTestRow]:
        rows: list[SelfTestRow] = []
        for item in items:
            mine = [r for r in results if r.item_id == item.id]
            outcomes = tuple(r.outcome.value for r in mine)
            context = context_for(item)
            if spec.kind == "fake:obedient":
                expected = expected_obedient_outcome(item, context)
                wanted = reachable_kinds(item, context)
                missing = sorted(
                    {kind for r in mine for kind in wanted if kind not in {f.kind for f in r.fired}}
                )
            else:
                expected = expected_safe_outcome(item, context)
                missing = []
            if not mine:
                missing = ["no attempts"]
            rows.append(SelfTestRow(item.id, expected.value, outcomes, tuple(missing)))
        return rows

    return check
