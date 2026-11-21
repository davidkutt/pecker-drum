"""The twin agent: LangChain ``create_agent`` with approval in front of the write tools."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, cast

from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, GraphOutput
from pydantic import JsonValue

from pecker_drum.items.canary import find_canaries
from pecker_drum.items.schema import User
from pecker_drum.observation import (
    ApprovalRecord,
    Observation,
    Source,
    ToolCallRecord,
    ToolCallStatus,
    Usage,
)
from pecker_drum.twin.tools import WRITE_TOOLS, TwinTools

DENY_REASON = "Denied by the harness: write actions are never approved during a test run."
_ERROR_CHARS = 300


def system_prompt() -> str:
    return files("pecker_drum.twin").joinpath("system_prompt.md").read_text(encoding="utf-8")


def system_prompt_canary() -> str:
    canaries = find_canaries(system_prompt())
    if len(canaries) != 1:
        msg = "the twin's system prompt must contain exactly one canary"
        raise RuntimeError(msg)
    return canaries[0]


@dataclass(slots=True)
class _Trace:
    rejected: dict[str, ApprovalRecord] = field(default_factory=dict[str, ApprovalRecord])
    answers: list[str] = field(default_factory=list[str])


def _text(message: BaseMessage) -> str:
    return str(message.text)


def _json_args(args: dict[str, Any]) -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", json.loads(json.dumps(args, default=str)))


def _source_id(name: str, args: dict[str, Any]) -> str:
    rendered = ",".join(f"{key}={value}" for key, value in sorted(args.items()))
    return f"{name}({rendered})"[:160]


def _usage(messages: Sequence[BaseMessage], latency_s: float) -> Usage:
    input_tokens = output_tokens = 0
    costs: list[float] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        if message.usage_metadata:
            input_tokens += message.usage_metadata.get("input_tokens", 0)
            output_tokens += message.usage_metadata.get("output_tokens", 0)
        token_usage = message.response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            cost = cast("dict[str, Any]", token_usage).get("cost")
            if isinstance(cost, int | float):
                costs.append(float(cost))
    ai_turns = sum(1 for m in messages if isinstance(m, AIMessage))
    cost_usd = sum(costs) if costs and len(costs) == ai_turns else None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_s=round(latency_s, 4),
    )


def build_observation(
    messages: Sequence[BaseMessage], trace: _Trace, latency_s: float, error: str | None
) -> Observation:
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    calls: list[ToolCallRecord] = []
    sources: list[Source] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            call_id = call.get("id") or ""
            if call_id in trace.rejected:
                status = ToolCallStatus.DENIED
            elif call_id in results:
                status = ToolCallStatus.EXECUTED
            else:
                status = ToolCallStatus.PROPOSED
            calls.append(
                ToolCallRecord(
                    id=call_id, name=call["name"], arguments=_json_args(call["args"]), status=status
                )
            )
            result = results.get(call_id)
            is_read = call["name"] not in WRITE_TOOLS
            if status is ToolCallStatus.EXECUTED and result is not None and is_read:
                source_id = _source_id(call["name"], call["args"])
                sources.append(Source(id=source_id, text=_text(result)))
    return Observation(
        answer="\n\n".join(answer for answer in trace.answers if answer),
        sources=tuple(sources),
        tool_calls=tuple(calls),
        approvals=tuple(trace.rejected[c.id] for c in calls if c.id in trace.rejected),
        usage=_usage(messages, latency_s),
        error=error,
    )


class Twin:
    """One throwaway twin instance: tools bound to one attempt's fixtures, fresh memory."""

    def __init__(self, model: BaseChatModel, tools: TwinTools, recursion_limit: int = 30) -> None:
        interrupt_on: dict[str, bool | InterruptOnConfig] = {
            name: InterruptOnConfig(allowed_decisions=["approve", "reject"])
            for name in sorted(WRITE_TOOLS)
        }
        self._agent: Any = create_agent(
            model=model,
            tools=tools.as_langchain_tools(),
            system_prompt=system_prompt(),
            middleware=[
                HumanInTheLoopMiddleware(interrupt_on=interrupt_on, interrupt_mode="per_call")
            ],
            checkpointer=InMemorySaver(),
        )
        self._config: RunnableConfig = {
            "configurable": {"thread_id": "attempt"},
            "recursion_limit": recursion_limit,
        }

    async def _messages(self) -> list[BaseMessage]:
        state = await self._agent.aget_state(self._config)
        values = cast("dict[str, Any]", state.values) if state.values else {}
        return list(cast("list[BaseMessage]", values.get("messages", [])))

    async def _turn(self, text: str, trace: _Trace) -> None:
        payload: dict[str, Any] | Command[Any] = {"messages": [HumanMessage(content=text)]}
        while True:
            result = cast(
                "GraphOutput[Any]",
                await self._agent.ainvoke(payload, self._config, version="v2"),
            )
            if not result.interrupts:
                break
            answers: dict[str, Any] = {}
            for interrupt in result.interrupts:
                request = cast("dict[str, Any]", interrupt.value)
                call_id = str(request.get("tool_call_id", ""))
                trace.rejected[call_id] = ApprovalRecord(
                    tool_call_id=call_id,
                    tool=str(request.get("name", "")),
                    decision="reject",
                    reason=DENY_REASON,
                )
                answers[interrupt.id] = {"type": "reject", "message": DENY_REASON}
            payload = Command(resume=answers)
        messages = await self._messages()
        last = messages[-1] if messages else None
        trace.answers.append(_text(last) if isinstance(last, AIMessage) else "")

    async def run(self, user: User, turns: Sequence[str]) -> Observation:
        trace = _Trace()
        started = time.perf_counter()
        error: str | None = None
        try:
            for turn in turns:
                await self._turn(turn, trace)
        except Exception as exc:  # the target failed; record it, never re-raise into the run
            error = f"{type(exc).__name__}: {str(exc)[:_ERROR_CHARS]}"
        messages = await self._messages()
        return build_observation(messages, trace, time.perf_counter() - started, error)
