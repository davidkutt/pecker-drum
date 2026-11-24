"""Map server-sent events to observation parts through the config's dotted field paths (M2).

Events are matched by their SSE ``event:`` name or by a type field in the JSON data. Unknown
events are ignored. A mapped event whose data is not JSON, or lacks a field the mapping needs, is
malformed: the attempt stops with an error. Tool results map their status through the config:
only ``executed`` values count as executed, and a value in no list is ``failed`` (ADR 0009). The
raw value travels along for the evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

from pydantic import JsonValue

from pecker_drum.observation import Source, ToolCallStatus
from pecker_drum.targets.http.config import Events
from pecker_drum.targets.http.sse import SseEvent

MAX_ID_CHARS = 200
MAX_STATUS_CHARS = 100


class MalformedEventError(Exception):
    """A mapped event the harness cannot read; the message names the event and the field."""


class MissingFieldError(KeyError):
    pass


def get_path(data: JsonValue, path: str) -> JsonValue:
    """Follow a dotted path through objects (keys) and lists (numeric indices)."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise MissingFieldError(path)
    return current


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    text: str


@dataclass(frozen=True, slots=True)
class SourcesFound:
    sources: tuple[Source, ...]


@dataclass(frozen=True, slots=True)
class ToolCallSeen:
    id: str | None
    name: str
    arguments: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class ToolResultSeen:
    tool_call_id: str
    status: ToolCallStatus
    raw_status: str = ""
    """The target's own status value, cut to 100 characters; empty without a status field."""


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    approval_id: str
    tool_call_id: str | None
    tool: str | None


@dataclass(frozen=True, slots=True)
class TargetReportedError:
    message: str


@dataclass(frozen=True, slots=True)
class StreamDone:
    pass


MappedEvent = (
    AnswerDelta
    | SourcesFound
    | ToolCallSeen
    | ToolResultSeen
    | ApprovalRequested
    | TargetReportedError
    | StreamDone
)


def _identifier(value: JsonValue, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        msg = f"{where} is not a string or an integer"
        raise MalformedEventError(msg)
    text = str(value)
    if not text or len(text) > MAX_ID_CHARS:
        msg = f"{where} is empty or longer than {MAX_ID_CHARS} characters"
        raise MalformedEventError(msg)
    return text


def _text(value: JsonValue, where: str) -> str:
    if not isinstance(value, str):
        msg = f"{where} is not a string"
        raise MalformedEventError(msg)
    return value


def _arguments(value: JsonValue) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"raw": value}
        if isinstance(parsed, dict):
            return cast("dict[str, JsonValue]", parsed)
        return {"raw": value}
    return {"raw": cast("JsonValue", value)}


class EventMapper:
    def __init__(self, events: Events) -> None:
        self._events = events
        self._kinds = {
            name: kind for kind, mapping in events.mappings().items() for name in mapping.names
        }

    def map(self, event: SseEvent) -> MappedEvent | None:
        """``None`` for an event the config does not map."""
        if self._events.match == "event":
            kind = self._kinds.get(event.event)
            if kind is None:
                return None
            return self._read(kind, event.event, self._json(event, event.event))
        data = self._json(event, event.event)
        type_field = self._events.type_field or ""
        try:
            value = get_path(data, type_field)
        except MissingFieldError:
            return None
        kind = self._kinds.get(value) if isinstance(value, str) else None
        if kind is None:
            return None
        return self._read(kind, cast("str", value), data)

    @staticmethod
    def _json(event: SseEvent, name: str) -> JsonValue:
        try:
            return cast("JsonValue", json.loads(event.data))
        except ValueError:
            msg = f"event {name[:40]!r}: data is not JSON"
            raise MalformedEventError(msg) from None

    def _get(self, data: JsonValue, path: str, name: str) -> JsonValue:
        try:
            return get_path(data, path)
        except MissingFieldError:
            msg = f"event {name[:40]!r}: missing field {path!r}"
            raise MalformedEventError(msg) from None

    def _optional(self, data: JsonValue, path: str | None) -> JsonValue:
        if path is None:
            return None
        try:
            return get_path(data, path)
        except MissingFieldError:
            return None

    def _read(self, kind: str, name: str, data: JsonValue) -> MappedEvent:
        events = self._events
        where = f"event {name[:40]!r}"
        if kind == "answer":
            return AnswerDelta(_text(self._get(data, events.answer.text, name), f"{where} text"))
        if kind == "sources" and events.sources is not None:
            mapping = events.sources
            container = self._get(data, mapping.items, name) if mapping.items else [data]
            if not isinstance(container, list):
                msg = f"{where}: {mapping.items!r} is not a list"
                raise MalformedEventError(msg)
            sources = tuple(
                Source(
                    id=_identifier(self._get(entry, mapping.id, name), f"{where} source id"),
                    text=_text(self._get(entry, mapping.text, name), f"{where} source text"),
                )
                for entry in container
            )
            return SourcesFound(sources)
        if kind == "tool_call" and events.tool_call is not None:
            mapping = events.tool_call
            call_id = self._optional(data, mapping.id)
            return ToolCallSeen(
                id=None if call_id is None else _identifier(call_id, f"{where} id"),
                name=_text(self._get(data, mapping.name, name), f"{where} name")[:MAX_ID_CHARS],
                arguments=_arguments(self._optional(data, mapping.arguments)),
            )
        if kind == "tool_result" and events.tool_result is not None:
            mapping = events.tool_result
            call_id = _identifier(self._get(data, mapping.tool_call_id, name), f"{where} id")
            if mapping.status is None:
                return ToolResultSeen(call_id, ToolCallStatus.EXECUTED)
            raw = self._get(data, mapping.status, name)
            status = raw if isinstance(raw, str) else json.dumps(raw)
            kept = status[:MAX_STATUS_CHARS]
            if status in mapping.executed:
                return ToolResultSeen(call_id, ToolCallStatus.EXECUTED, kept)
            if status in mapping.rejected:
                return ToolResultSeen(call_id, ToolCallStatus.DENIED, kept)
            if status in mapping.refused:
                return ToolResultSeen(call_id, ToolCallStatus.REFUSED, kept)
            return ToolResultSeen(call_id, ToolCallStatus.FAILED, kept)
        if kind == "approval" and events.approval is not None:
            mapping = events.approval
            call_id = self._optional(data, mapping.tool_call_id)
            tool = self._optional(data, mapping.tool)
            return ApprovalRequested(
                approval_id=_identifier(self._get(data, mapping.id, name), f"{where} id"),
                tool_call_id=None if call_id is None else _identifier(call_id, f"{where} call"),
                tool=tool[:MAX_ID_CHARS] if isinstance(tool, str) and tool else None,
            )
        if kind == "error":
            mapping = events.error
            message = self._optional(data, mapping.message if mapping else None)
            text = message if isinstance(message, str) else json.dumps(data, ensure_ascii=False)
            return TargetReportedError(text[:500])
        return StreamDone()
