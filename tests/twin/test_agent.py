from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, ToolCall
from langchain_core.outputs import ChatResult

from pecker_drum.items.schema import User
from pecker_drum.observation import ToolCallStatus
from pecker_drum.twin.agent import DENY_REASON, Twin, system_prompt, system_prompt_canary
from pecker_drum.twin.embeddings import HashEmbedding
from pecker_drum.twin.fixtures import FixtureLibrary
from pecker_drum.twin.kb import KnowledgeBase
from pecker_drum.twin.models import ScriptedChatModel
from pecker_drum.twin.tools import TwinTools

CAROL = User(sub="carol")


def tools(mini_fixtures: Path) -> TwinTools:
    library = FixtureLibrary(mini_fixtures)
    scenario = library.base_scenario().overlay(library.scenario("demo"))
    kb = KnowledgeBase(library.base_documents(), HashEmbedding())
    return TwinTools(kb, scenario, tuple(library.base_pages()), CAROL)


def call(call_id: str, name: str, **args: Any) -> ToolCall:
    return ToolCall(name=name, args=args, id=call_id, type="tool_call")


def scripted(*steps: AIMessage, final: str = "final answer") -> ScriptedChatModel:
    return ScriptedChatModel(steps=list(steps), final_answer=final)


def test_system_prompt_has_one_canary_and_the_untrusted_rule() -> None:
    assert system_prompt_canary() in system_prompt()
    assert "untrusted data" in system_prompt()


async def test_write_tools_are_proposed_and_denied(mini_fixtures: Path) -> None:
    model = scripted(
        AIMessage(content="", tool_calls=[call("a", "repo_get_issue", number=10)]),
        AIMessage(
            content="",
            tool_calls=[
                call("b", "repo_create_issue", title="t", body="b"),
                call("c", "kb_ingest_url", url="https://notes.example/x"),
            ],
        ),
    )
    observation = await Twin(model, tools(mini_fixtures)).run(CAROL, ["status of #10?"])
    statuses = {c.name: c.status for c in observation.tool_calls}
    assert statuses == {
        "repo_get_issue": ToolCallStatus.EXECUTED,
        "repo_create_issue": ToolCallStatus.DENIED,
        "kb_ingest_url": ToolCallStatus.DENIED,
    }
    assert [(a.tool, a.decision, a.reason) for a in observation.approvals] == [
        ("repo_create_issue", "reject", DENY_REASON),
        ("kb_ingest_url", "reject", DENY_REASON),
    ]
    assert [s.id for s in observation.sources] == ["repo_get_issue(number=10)"]
    assert "CANARY-0000000000c3" in observation.sources[0].text
    assert observation.answer == "final answer"
    assert observation.error is None
    assert observation.usage.input_tokens > 0


async def test_unknown_lookups_return_text_not_errors(mini_fixtures: Path) -> None:
    model = scripted(
        AIMessage(
            content="",
            tool_calls=[
                call("a", "fetch_url", url="https://elsewhere.example/"),
                call("b", "repo_get_commit", sha="9c1e4b7"),
                call("c", "repo_get_ci_log", run_id=5150),
                call("d", "repo_get_file", path="/.github/workflows/deploy.yml"),
            ],
        )
    )
    observation = await Twin(model, tools(mini_fixtures)).run(CAROL, ["look"])
    texts = [s.text for s in observation.sources]
    assert texts[0].startswith("Could not fetch")
    assert "fix: logging" in texts[1]
    assert "step 2 failed" in texts[2]
    assert "name: deploy" in texts[3]


async def test_multi_turn_answers_are_joined(mini_fixtures: Path) -> None:
    model = scripted(AIMessage(content="first"), final="second")
    observation = await Twin(model, tools(mini_fixtures)).run(CAROL, ["one", "two"])
    assert observation.answer == "first\n\nsecond"


class _Exploding(ScriptedChatModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if any(isinstance(m, AIMessage) for m in messages):
            msg = "provider unavailable"
            raise ConnectionError(msg)
        return super()._generate(messages, stop, run_manager, **kwargs)


async def test_errors_are_recorded_with_partial_trace(mini_fixtures: Path) -> None:
    model = _Exploding(
        steps=[AIMessage(content="", tool_calls=[call("a", "repo_get_issue", number=10)])],
        final_answer="never",
    )
    observation = await Twin(model, tools(mini_fixtures)).run(CAROL, ["x"])
    assert observation.error == "ConnectionError: provider unavailable"
    assert [c.status for c in observation.tool_calls] == [ToolCallStatus.EXECUTED]


async def test_runaway_tool_loops_hit_the_recursion_limit(mini_fixtures: Path) -> None:
    loop = [
        AIMessage(content="", tool_calls=[call(f"c{i}", "repo_get_issue", number=1)])
        for i in range(50)
    ]
    observation = await Twin(scripted(*loop), tools(mini_fixtures), recursion_limit=8).run(
        CAROL, ["x"]
    )
    assert observation.error is not None
    assert "GraphRecursionError" in observation.error
