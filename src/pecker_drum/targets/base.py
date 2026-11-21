"""The target protocol: run one item, return one observation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pecker_drum.grading.predicates import GradingContext
from pecker_drum.items.schema import Item
from pecker_drum.observation import Observation


@runtime_checkable
class Target(Protocol):
    @property
    def name(self) -> str:
        """Short name used in run ids and report file names (``twin``)."""
        ...

    def describe(self) -> dict[str, str]:
        """Run settings worth printing in the report header (no secrets)."""
        ...

    async def run(self, item: Item, attempt: int) -> Observation:
        """Run ``item`` once. Must not raise for target failures: set ``Observation.error``."""
        ...

    def grading_context(self, item: Item) -> GradingContext:
        """Canaries, allowed hosts and gated tools the grader needs for ``item``."""
        ...
