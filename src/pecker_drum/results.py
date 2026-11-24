"""Per-attempt results and run metadata, shared by the campaign runner and the reports."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from pecker_drum.grading.outcome import Grade, Outcome
from pecker_drum.observation import Observation, Usage
from pecker_drum.report.excerpt import escape_answer, escape_excerpt

if TYPE_CHECKING:  # the matcher imports the two enums above: no runtime cycle
    from pecker_drum.grading.retrieval import Retrieval


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalState(StrEnum):
    """Whether a planted fixture was among the sources the target returned (M2.1)."""

    RETRIEVED = "retrieved"
    NOT_RETRIEVED = "not_retrieved"
    UNKNOWN = "unknown"
    """The target exposes no sources channel, or a planted fixture had no usable signal."""


class RetrievalSignal(StrEnum):
    """The marker that matched, when one did (M2.1): never set unless ``retrieved``."""

    ID = "id"
    CANARY = "canary"
    BOTH = "both"


class JudgeLabel(StrEnum):
    """The advisory judge's view of an attempt the strict grader called success or contained."""

    COMPLIED = "complied"
    """The target carried out the attacker's goal."""
    REPORTED = "reported"
    """The target only mentioned, quoted or warned about the attack."""
    UNCLEAR = "unclear"
    """The judge could not tell, or its reply was not a valid label (judge error)."""


class JudgeVerdict(_Frozen):
    """Advisory only (ADR 0008): never changes an outcome or a rate."""

    label: JudgeLabel
    reason: str = ""
    """Escaped excerpt of the judge's one-sentence reason (judge output is hostile too)."""
    usage: Usage = Usage()


class FiredPredicate(_Frozen):
    kind: str
    channel: str
    evidence: str
    """Escaped excerpt, at most 120 characters."""


def _source_ids(observation: Observation) -> tuple[str, ...]:
    """The ids the target returned, in its order, deduplicated, then escaped and cut."""
    raw = dict.fromkeys(source.id for source in observation.sources)
    return tuple(escape_excerpt(source_id) for source_id in raw)


class AttemptResult(_Frozen):
    item_id: str
    attempt: int
    outcome: Outcome
    fired: tuple[FiredPredicate, ...] = ()
    answer_excerpt: str = ""
    """Escaped excerpt of the answer, at most 120 characters."""
    answer: str | None = None
    """The full answer, escaped but uncut (M1.5). ``None``: recorded before M1.5."""
    source_ids: tuple[str, ...] = ()
    """Ids of the sources the target returned, in its order, without duplicates, escaped and cut
    to 120 characters (M2.1). Source texts stay out: they can be long and private."""
    retrieved: RetrievalState | None = None
    """Whether a planted fixture was among the sources (M2.1). ``None``: the item planted
    nothing, or the attempt was recorded before M2.1."""
    retrieval_signal: RetrievalSignal | None = None
    """The signal that matched: ``id``, ``canary`` or ``both`` (M2.1). ``None`` unless
    ``retrieved``."""
    tool_calls: tuple[str, ...] = ()
    """``name:status`` for every tool call, in order."""
    error: str | None = None
    """Escaped excerpt of the error, at most 120 characters."""
    usage: Usage = Usage()
    judge: JudgeVerdict | None = None
    """Set only when a judge ran on a success or contained attempt."""
    unobservable: tuple[str, ...] = ()
    """Predicate kinds that need a channel the target does not expose (M2, ADR 0009)."""
    contradictions: tuple[str, ...] = ()
    """Escaped excerpts: a call the harness denied that the target reports as executed (M2,
    ADR 0009). Empty in records written before the field existed."""

    @classmethod
    def from_grade(
        cls,
        item_id: str,
        attempt: int,
        observation: Observation,
        grade: Grade,
        retrieval: Retrieval | None = None,
    ) -> AttemptResult:
        """``retrieval``: what the sources say about the planted fixtures (M2.1, ADR 0010);
        ``None`` leaves the three fields unset (an item that plants nothing)."""
        return cls(
            item_id=item_id,
            attempt=attempt,
            outcome=grade.outcome,
            fired=tuple(
                FiredPredicate(
                    kind=result.kind,
                    channel=result.channel.value,
                    evidence=escape_excerpt(result.evidence),
                )
                for result in grade.fired
            ),
            answer_excerpt=escape_excerpt(observation.answer),
            answer=escape_answer(observation.answer),
            source_ids=_source_ids(observation),
            retrieved=retrieval.state if retrieval is not None else None,
            retrieval_signal=retrieval.signal if retrieval is not None else None,
            tool_calls=tuple(f"{c.name}:{c.status.value}" for c in observation.tool_calls),
            error=escape_excerpt(observation.error) if observation.error else None,
            usage=observation.usage,
            unobservable=grade.unobservable,
            contradictions=tuple(escape_excerpt(c) for c in observation.contradictions),
        )


class SkippedItem(_Frozen):
    """An item outside the run's ``--vectors`` filter (M2)."""

    id: str
    vector: str


class RunInfo(_Frozen):
    run_id: str
    target: str
    item_set: str
    set_digest: str
    model: str
    repeats: int
    settings: dict[str, str]
    started_at: str
    finished_at: str = ""
    harness_version: str
    judge: str = ""
    """The judge model (``fake:reported``, ``openrouter:<id>``); empty without a judge."""
    vectors: tuple[str, ...] = ()
    """The ``--vectors`` filter, sorted; empty when every vector runs (M2)."""
    skipped: tuple[SkippedItem, ...] = ()
    """Items of the set outside the filter, in set order."""
