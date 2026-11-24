"""What a target returns for one attempt. Everything in it is hostile data."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservedChannel(StrEnum):
    """What a target exposes to the harness (M2). A predicate needs certain channels; if one of
    them is missing and nothing fired, the attempt is ``unobservable`` (ADR 0009)."""

    ANSWER = "answer"
    SOURCES = "sources"
    TOOL_CALLS = "tool_calls"
    """Tool calls the model proposed."""
    TOOL_RESULTS = "tool_results"
    """Whether a proposed call ran (needed to tell ``executed`` from ``proposed``)."""
    APPROVALS = "approvals"
    """Approval requests the harness can see and deny."""


ALL_CHANNELS: frozenset[ObservedChannel] = frozenset(ObservedChannel)
"""The twin exposes everything."""


class ToolCallStatus(StrEnum):
    PROPOSED = "proposed"
    """The model asked for the tool; no decision or result was recorded."""
    EXECUTED = "executed"
    DENIED = "denied"
    """An approval step rejected the call; the tool did not run."""
    REFUSED = "refused"
    """The target's own permission check stopped the call (a control, like a denied approval)."""
    FAILED = "failed"
    """The target reported a status outside its executed, rejected and refused lists (bad
    arguments, a timeout, a limit). The model made the call, so it counts for
    ``tool_proposed`` (ADR 0009); the target's own status is kept in ``target_status``."""


class ToolCallRecord(_Frozen):
    id: str = ""
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    status: ToolCallStatus
    target_status: str = ""
    """The status the target reported for the call's result, as given (M2); empty when it
    reported none. Hostile: escape before showing it."""


class ApprovalRecord(_Frozen):
    tool_call_id: str
    tool: str
    decision: Literal["approve", "reject"]
    reason: str = ""


class Source(_Frozen):
    """A retrieved chunk or document the target used (or showed) for its answer."""

    id: str
    text: str


class Usage(_Frozen):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    """``None`` when the provider did not report a cost."""
    latency_s: float = 0.0


class Observation(_Frozen):
    answer: str = ""
    sources: tuple[Source, ...] = ()
    tool_calls: tuple[ToolCallRecord, ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    usage: Usage = Usage()
    error: str | None = None
    observed: frozenset[ObservedChannel] = ALL_CHANNELS
    """The channels this target exposes (M2); the twin exposes all of them."""
    contradictions: tuple[str, ...] = ()
    """What the target reported against what the harness did (M2, ADR 0009): a call the
    harness denied that the target reports as executed. The call is graded as executed."""
