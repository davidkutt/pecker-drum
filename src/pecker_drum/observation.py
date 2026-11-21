"""What a target returns for one attempt. Everything in it is hostile data."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallStatus(StrEnum):
    PROPOSED = "proposed"
    """The model asked for the tool; no decision was recorded."""
    EXECUTED = "executed"
    DENIED = "denied"
    """An approval step rejected the call; the tool did not run."""


class ToolCallRecord(_Frozen):
    id: str = ""
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    status: ToolCallStatus


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
