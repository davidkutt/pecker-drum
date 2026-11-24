import json
from typing import Any

import pytest

from pecker_drum.observation import Source, ToolCallStatus
from pecker_drum.targets.http.config import HttpTargetConfig
from pecker_drum.targets.http.events import (
    AnswerDelta,
    ApprovalRequested,
    EventMapper,
    MalformedEventError,
    SourcesFound,
    StreamDone,
    TargetReportedError,
    ToolCallSeen,
    ToolResultSeen,
    get_path,
)
from pecker_drum.targets.http.sse import SseEvent


def event(name: str, data: Any) -> SseEvent:
    return SseEvent(name, data if isinstance(data, str) else json.dumps(data))


@pytest.fixture
def mapper(example_config: HttpTargetConfig) -> EventMapper:
    return EventMapper(example_config.events)


def test_mapped_events(mapper: EventMapper) -> None:
    assert mapper.map(event("token", {"delta": "Hi"})) == AnswerDelta("Hi")
    docs = {"documents": [{"id": 7, "text": "runbook"}, {"id": "d2", "text": "faq"}]}
    assert mapper.map(event("sources", docs)) == SourcesFound(
        (Source(id="7", text="runbook"), Source(id="d2", text="faq"))
    )
    call = {"id": "c1", "name": "kb_search", "arguments": '{"query": "cache"}'}
    assert mapper.map(event("tool_call", call)) == ToolCallSeen(
        "c1", "kb_search", {"query": "cache"}
    )
    approval = {"approval_id": "a1", "tool_call_id": "c1", "tool": "repo_create_issue"}
    assert mapper.map(event("approval_required", approval)) == ApprovalRequested(
        "a1", "c1", "repo_create_issue"
    )
    assert mapper.map(event("approval_required", {"approval_id": "a2"})) == ApprovalRequested(
        "a2", None, None
    )
    assert mapper.map(event("error", {"message": "quota"})) == TargetReportedError("quota")
    assert mapper.map(event("error", {"detail": "x"})) == TargetReportedError('{"detail": "x"}')
    assert mapper.map(event("done", {})) == StreamDone()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ok", ToolCallStatus.EXECUTED),
        ("rejected", ToolCallStatus.DENIED),
        ("forbidden", ToolCallStatus.REFUSED),
        ("error", ToolCallStatus.FAILED),
        ("OK", ToolCallStatus.FAILED),  # only configured values count as executed
        (200, ToolCallStatus.FAILED),
    ],
)
def test_tool_result_statuses(mapper: EventMapper, status: Any, expected: ToolCallStatus) -> None:
    result = mapper.map(event("tool_result", {"tool_call_id": "c1", "status": status}))
    raw = status if isinstance(status, str) else json.dumps(status)
    assert result == ToolResultSeen("c1", expected, raw)  # the target's own value travels along


def test_a_long_status_is_cut(mapper: EventMapper) -> None:
    result = mapper.map(event("tool_result", {"tool_call_id": "c1", "status": "x" * 500}))
    assert result == ToolResultSeen("c1", ToolCallStatus.FAILED, "x" * 100)


def test_unknown_events_are_ignored_without_parsing(mapper: EventMapper) -> None:
    assert mapper.map(event("ping", "not json at all")) is None
    assert mapper.map(event("message", "{}")) is None


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("token", "not json", "data is not JSON"),
        ("token", {"text": "wrong field"}, "missing field 'delta'"),
        ("token", {"delta": 5}, "not a string"),
        ("sources", {"documents": "nope"}, "not a list"),
        ("sources", {"documents": [{"id": "x"}]}, "missing field 'text'"),
        ("tool_call", {"id": "c1"}, "missing field 'name'"),
        ("tool_result", {"status": "ok"}, "missing field 'tool_call_id'"),
        ("tool_result", {"tool_call_id": True, "status": "ok"}, "not a string or an integer"),
        ("approval_required", {"tool": "x"}, "missing field 'approval_id'"),
        ("approval_required", {"approval_id": "a" * 201}, "longer than 200"),
    ],
)
def test_malformed_mapped_events(mapper: EventMapper, name: str, data: Any, message: str) -> None:
    with pytest.raises(MalformedEventError, match=message):
        mapper.map(event(name, data))


def test_matching_on_a_type_field(example_config: HttpTargetConfig) -> None:
    events = example_config.events.model_copy(update={"match": "field", "type_field": "kind"})
    mapper = EventMapper(events)
    assert mapper.map(event("message", {"kind": "token", "delta": "x"})) == AnswerDelta("x")
    assert mapper.map(event("message", {"kind": "heartbeat"})) is None
    assert mapper.map(event("message", {"no_kind": 1})) is None
    with pytest.raises(MalformedEventError, match="not JSON"):
        mapper.map(event("message", "[DONE"))


def test_get_path() -> None:
    data: Any = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert get_path(data, "a.b.1.c") == 2
    with pytest.raises(KeyError):
        get_path(data, "a.b.2.c")
