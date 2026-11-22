"""Per-attempt results and run metadata, shared by the campaign runner and the reports."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pecker_drum.grading.outcome import Grade, Outcome
from pecker_drum.observation import Observation, Usage
from pecker_drum.report.excerpt import escape_answer, escape_excerpt


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class AttemptResult(_Frozen):
    item_id: str
    attempt: int
    outcome: Outcome
    fired: tuple[FiredPredicate, ...] = ()
    answer_excerpt: str = ""
    """Escaped excerpt of the answer, at most 120 characters."""
    answer: str | None = None
    """The full answer, escaped but uncut (M1.5). ``None``: recorded before M1.5."""
    tool_calls: tuple[str, ...] = ()
    """``name:status`` for every tool call, in order."""
    error: str | None = None
    """Escaped excerpt of the error, at most 120 characters."""
    usage: Usage = Usage()
    judge: JudgeVerdict | None = None
    """Set only when a judge ran on a success or contained attempt."""

    @classmethod
    def from_grade(
        cls, item_id: str, attempt: int, observation: Observation, grade: Grade
    ) -> AttemptResult:
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
            tool_calls=tuple(f"{c.name}:{c.status.value}" for c in observation.tool_calls),
            error=escape_excerpt(observation.error) if observation.error else None,
            usage=observation.usage,
        )


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
