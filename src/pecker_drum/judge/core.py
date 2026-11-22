"""The advisory judge (M1.5, ADR 0008): complied, reported or unclear. Never an outcome.

The judge runs only on attempts the strict grader called ``success`` or ``contained`` and adds a
label with a one-sentence reason. Outcomes, rates and the self-test never read it. Whatever goes
wrong (an exception, a reply that is not valid JSON, a label outside the enum) becomes ``unclear``
with the reason "judge error", and the judge's own reply is treated as hostile like target output:
escaped and cut to an excerpt.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from pecker_drum.grading.outcome import Outcome
from pecker_drum.items.schema import Item
from pecker_drum.judge.prompt import (
    JUDGE_RESPONSE_FORMAT,
    JUDGE_SYSTEM_PROMPT,
    JudgeCase,
    goal_text,
    judge_user_message,
)
from pecker_drum.observation import Usage
from pecker_drum.report.excerpt import escape_excerpt
from pecker_drum.results import AttemptResult, JudgeLabel, JudgeVerdict
from pecker_drum.twin.models import ModelSpecError, openrouter_model

if TYPE_CHECKING:
    import httpx
    from langchain_openai import ChatOpenAI

JudgeKind = Literal["fake:complied", "fake:reported", "openrouter"]
JUDGED_OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.CONTAINED})
JUDGE_PROVIDER_PREFS = {"require_parameters": True}
"""Route only to providers that honour every request parameter (the strict response schema)."""
NOT_RECORDED = "full answer not recorded (attempt from before M1.5)"


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    kind: JudgeKind
    model_id: str = ""

    @property
    def is_fake(self) -> bool:
        return self.kind != "openrouter"

    def __str__(self) -> str:
        return f"openrouter:{self.model_id}" if self.kind == "openrouter" else self.kind


def parse_judge_spec(value: str) -> JudgeSpec:
    if value in ("fake:complied", "fake:reported"):
        return JudgeSpec(value)
    prefix, _, model_id = value.partition(":")
    if prefix == "openrouter" and model_id.strip():
        return JudgeSpec("openrouter", model_id.strip())
    msg = f"unknown judge {value!r}: use fake:complied, fake:reported or openrouter:<model id>"
    raise ModelSpecError(msg)


class Judge(Protocol):
    @property
    def name(self) -> str: ...

    async def judge(self, case: JudgeCase) -> JudgeVerdict: ...


class FakeJudge:
    """Always the same label: for tests and self-tests, never touches the network."""

    def __init__(self, label: JudgeLabel) -> None:
        self._label = label

    @property
    def name(self) -> str:
        return f"fake:{self._label.value}"

    async def judge(self, case: JudgeCase) -> JudgeVerdict:
        return JudgeVerdict(label=self._label, reason=f"fake judge: always {self._label.value}")


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: JudgeLabel
    reason: str


def parse_reply(content: object) -> tuple[JudgeLabel, str]:
    """The judge's JSON reply; anything that is not exactly a label and a reason is unclear."""
    if not isinstance(content, str):
        return JudgeLabel.UNCLEAR, "judge error: the reply is not text"
    try:
        reply = _Reply.model_validate_json(content)
    except ValidationError:
        return JudgeLabel.UNCLEAR, "judge error: the reply is not a valid label and reason"
    return reply.label, reply.reason


def _usage(message: AIMessage, latency_s: float) -> Usage:
    usage = message.usage_metadata or {}
    token_usage: object = message.response_metadata.get("token_usage")
    cost: object = (
        cast("Mapping[str, object]", token_usage).get("cost")
        if isinstance(token_usage, Mapping)
        else None
    )
    return Usage(
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        latency_s=round(latency_s, 4),
    )


class OpenRouterJudge:
    """An OpenRouter model at temperature 0 with a strict JSON schema whose label is an enum.

    Requests carry the same zero-data-retention preferences as live targets.
    """

    def __init__(
        self, model_id: str, *, http_async_client: httpx.AsyncClient | None = None
    ) -> None:
        self._name = f"openrouter:{model_id}"
        self._model: ChatOpenAI = openrouter_model(
            model_id,
            http_async_client=http_async_client,
            temperature=0.0,
            provider_extra=JUDGE_PROVIDER_PREFS,
        )

    @property
    def name(self) -> str:
        return self._name

    async def judge(self, case: JudgeCase) -> JudgeVerdict:
        messages = [SystemMessage(JUDGE_SYSTEM_PROMPT), HumanMessage(judge_user_message(case))]
        started = time.perf_counter()
        reply = await self._model.ainvoke(messages, response_format=dict(JUDGE_RESPONSE_FORMAT))
        label, reason = parse_reply(reply.content)
        return JudgeVerdict(
            label=label,
            reason=escape_excerpt(reason),
            usage=_usage(reply, time.perf_counter() - started),
        )


def make_judge(spec: JudgeSpec, *, http_async_client: httpx.AsyncClient | None = None) -> Judge:
    """One judge per run. An OpenRouter judge reads ``OPENROUTER_API_KEY`` here."""
    if spec.kind == "fake:complied":
        return FakeJudge(JudgeLabel.COMPLIED)
    if spec.kind == "fake:reported":
        return FakeJudge(JudgeLabel.REPORTED)
    return OpenRouterJudge(spec.model_id, http_async_client=http_async_client)


def case_for(item: Item, result: AttemptResult) -> JudgeCase:
    return JudgeCase(
        title=item.title,
        goals=tuple(goal_text(predicate) for predicate in item.succeeds_if),
        fired=tuple(dict.fromkeys(fired.kind for fired in result.fired)),
        outcome=result.outcome.value,
        turns=tuple(item.turns),
        tool_calls=result.tool_calls,
        answer=result.answer or "",
    )


async def judge_result(judge: Judge, item: Item, result: AttemptResult) -> AttemptResult:
    """Label a success or contained attempt; every other outcome gets no label.

    The outcome itself is never touched. A judge failure becomes ``unclear`` ("judge error").
    """
    if result.outcome not in JUDGED_OUTCOMES:
        return result.model_copy(update={"judge": None})
    if result.answer is None:
        verdict = JudgeVerdict(label=JudgeLabel.UNCLEAR, reason=NOT_RECORDED)
    else:
        try:
            verdict = await judge.judge(case_for(item, result))
        except Exception as exc:  # a judge must never cost the run or change an outcome
            reason = escape_excerpt(f"judge error: {type(exc).__name__}")
            verdict = JudgeVerdict(label=JudgeLabel.UNCLEAR, reason=reason)
    return result.model_copy(update={"judge": verdict})
