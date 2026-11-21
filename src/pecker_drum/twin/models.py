"""Model modes for the twin: ``fake:obedient``, ``fake:safe`` and ``openrouter:<id>``.

The fake models are scripted from an item's ``self_test`` and never touch the network. The
OpenRouter model always sends ``provider.zdr = true`` (zero data retention).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, ToolCall
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from pecker_drum.items.schema import Item
from pecker_drum.twin.tools import WRITE_TOOLS

if TYPE_CHECKING:
    import httpx

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
ZDR_PROVIDER_PREFS: Mapping[str, object] = {"zdr": True, "data_collection": "deny"}

SAFE_ANSWER = (
    "Here is a short summary based on the sources I could read. They contain requests aimed at "
    "automated assistants; I did not act on them."
)
OBEDIENT_DEFAULT_ANSWER = "Done."

ModelKind = Literal["fake:obedient", "fake:safe", "openrouter"]


class ModelSpecError(ValueError):
    """The ``--model`` value is not understood."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    kind: ModelKind
    model_id: str = ""

    @property
    def is_fake(self) -> bool:
        return self.kind != "openrouter"

    def __str__(self) -> str:
        return f"openrouter:{self.model_id}" if self.kind == "openrouter" else self.kind


def parse_model_spec(value: str) -> ModelSpec:
    if value == "fake:obedient":
        return ModelSpec("fake:obedient")
    if value == "fake:safe":
        return ModelSpec("fake:safe")
    prefix, _, model_id = value.partition(":")
    if prefix == "openrouter" and model_id.strip():
        return ModelSpec("openrouter", model_id.strip())
    msg = f"unknown model {value!r}: use fake:obedient, fake:safe or openrouter:<model id>"
    raise ModelSpecError(msg)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ScriptedChatModel(BaseChatModel):
    """Replays a fixed list of AI turns: step n answers the n-th model call of a conversation."""

    steps: list[AIMessage]
    final_answer: str

    @property
    def _llm_type(self) -> str:
        return "pecker-scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        step = sum(1 for message in messages if isinstance(message, AIMessage))
        template = (
            self.steps[step] if step < len(self.steps) else AIMessage(content=self.final_answer)
        )
        prompt_chars = sum(len(str(message.content)) for message in messages)
        reply = template.model_copy(
            update={
                "usage_metadata": {
                    "input_tokens": max(1, prompt_chars // 4),
                    "output_tokens": _approx_tokens(str(template.content) or "x"),
                    "total_tokens": max(1, prompt_chars // 4)
                    + _approx_tokens(str(template.content) or "x"),
                },
                "response_metadata": {"token_usage": {"cost": 0.0}},
            }
        )
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _tool_turn(index: int, name: str, args: dict[str, Any]) -> AIMessage:
    call = ToolCall(name=name, args=args, id=f"call_{index}", type="tool_call")
    return AIMessage(content="", tool_calls=[call])


def obedient_model(item: Item) -> ScriptedChatModel:
    """Follows the attacker: replays the item's ``self_test`` tool calls, then its answer."""
    steps = [
        _tool_turn(index, call.name, dict(call.args))
        for index, call in enumerate(item.self_test.tool_calls)
    ]
    return ScriptedChatModel(
        steps=steps, final_answer=item.self_test.answer or OBEDIENT_DEFAULT_ANSWER
    )


def safe_model(item: Item) -> ScriptedChatModel:
    """Reads what the obedient model reads, never calls a write tool, answers neutrally."""
    steps = [
        _tool_turn(index, call.name, dict(call.args))
        for index, call in enumerate(item.self_test.tool_calls)
        if call.name not in WRITE_TOOLS
    ]
    return ScriptedChatModel(steps=steps, final_answer=SAFE_ANSWER)


class MissingApiKeyError(RuntimeError):
    pass


def openrouter_model(
    model_id: str,
    *,
    api_key: str | None = None,
    http_async_client: httpx.AsyncClient | None = None,
    timeout_s: float = 120.0,
) -> ChatOpenAI:
    """An OpenRouter chat model that only routes to zero-data-retention endpoints."""
    key = api_key if api_key is not None else os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        msg = f"{OPENROUTER_KEY_ENV} is not set (read it with `read -s` / `read -s -x`)"
        raise MissingApiKeyError(msg)
    model = ChatOpenAI(
        model=model_id,
        base_url=OPENROUTER_BASE_URL,
        api_key=SecretStr(key),
        timeout=timeout_s,
        max_retries=2,
        use_responses_api=False,
        extra_body={"provider": dict(ZDR_PROVIDER_PREFS), "usage": {"include": True}},
        http_async_client=http_async_client,
    )
    assert_zdr(model)
    return model


def assert_zdr(model: ChatOpenAI) -> None:
    """Refuse to run a live model whose requests would not demand zero data retention."""
    body = model.extra_body or {}
    provider = body.get("provider")
    if not isinstance(provider, Mapping) or provider.get("zdr") is not True:  # pyright: ignore[reportUnknownMemberType]
        msg = "OpenRouter requests must set provider.zdr = true"
        raise RuntimeError(msg)


def model_for(spec: ModelSpec, item: Item, live_model: BaseChatModel | None) -> BaseChatModel:
    if spec.kind == "fake:obedient":
        return obedient_model(item)
    if spec.kind == "fake:safe":
        return safe_model(item)
    if live_model is None:
        msg = "a live model must be created once per run and passed in"
        raise RuntimeError(msg)
    return live_model
