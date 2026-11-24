"""The HTTP target adapter: attack an API target from the outside (M2, ADR 0009).

Per attempt: plant the item's ``doc:`` fixtures (the tag ``drum-fixture-<run id>`` in the file
name and title) as ``[fixtures] user`` or else the item's user, wait until the list endpoint
reports them indexed, create a conversation, send the turns as the item's user, deny every
approval request once its stream has closed (the deny response is a new event stream that
continues the turn), then delete the documents (as their owner) and the conversation in a
``finally``, also after an error or Ctrl-C. A document that cannot be deleted stops the run after
the attempt.

Per run (``RunLifecycle``): refuse to start while any ``drum-fixture-*`` document exists, and at
the end delete what is left with this run's tag and list again; ``remaining`` holds what is still
there. Everything the target returns is hostile, and every string in an observation passes the
redactor, so no password or token can reach a report.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from pecker_drum.grading.predicates import GradingContext, KnownCanary
from pecker_drum.items.canary import find_canaries
from pecker_drum.items.schema import Item
from pecker_drum.observation import (
    ApprovalRecord,
    Observation,
    ObservedChannel,
    Source,
    ToolCallRecord,
    ToolCallStatus,
    Usage,
)
from pecker_drum.targets.http.auth import TokenManager, read_secrets
from pecker_drum.targets.http.client import (
    Clock,
    HttpSession,
    Redactor,
    Sleep,
    TargetError,
    TargetTimeoutError,
)
from pecker_drum.targets.http.config import HttpTargetConfig
from pecker_drum.targets.http.events import (
    AnswerDelta,
    ApprovalRequested,
    EventMapper,
    MalformedEventError,
    MissingFieldError,
    SourcesFound,
    StreamDone,
    TargetReportedError,
    ToolCallSeen,
    ToolResultSeen,
    get_path,
)
from pecker_drum.targets.http.sse import SseEvent
from pecker_drum.targets.http.templates import render, render_json, render_path
from pecker_drum.twin.fixtures import FixtureError, FixtureLibrary

TAG_PREFIX = "drum-fixture-"
CONTRADICTION = "denied by the harness, reported executed by the target"
_ANY_TAG = re.compile(r"drum-fixture-([0-9a-f]{16})")
_MAX_ERROR = 300


@dataclass(frozen=True, slots=True)
class RemoteDocument:
    id: str
    tag_value: str
    """The value of the tag field (the uploaded file name)."""
    status: str = ""

    @property
    def run_id(self) -> str | None:
        match = _ANY_TAG.search(self.tag_value)
        return match.group(1) if match else None


class LeftoversError(RuntimeError):
    """Planted documents are on the target where none should be."""

    def __init__(self, documents: Sequence[RemoteDocument], *, at_start: bool) -> None:
        self.documents = tuple(documents)
        self.run_ids = tuple(sorted({d.run_id for d in documents if d.run_id}))
        names = ", ".join(d.tag_value[:120] for d in documents[:10])
        more = f" and {len(documents) - 10} more" if len(documents) > 10 else ""
        when = "before the run" if at_start else "after the run"
        super().__init__(
            f"{len(documents)} planted documents are on the target {when}: {names}{more}"
        )


@dataclass(slots=True)
class _Planted:
    filename: str
    user: str
    id: str | None = None


@dataclass(slots=True)
class _Call:
    id: str
    name: str
    arguments: dict[str, JsonValue]
    status: ToolCallStatus = ToolCallStatus.PROPOSED
    target_status: str = ""
    denied_by_harness: bool = False


@dataclass(slots=True)
class _Collector:
    """The observation of one attempt, built while the streams arrive."""

    observed: frozenset[ObservedChannel]
    turns: list[list[str]] = field(default_factory=list[list[str]])
    sources: list[Source] = field(default_factory=list[Source])
    calls: dict[str, _Call] = field(default_factory=dict[str, _Call])
    approvals: list[ApprovalRecord] = field(default_factory=list[ApprovalRecord])
    contradictions: list[str] = field(default_factory=list[str])
    error: str | None = None

    def fail(self, message: str) -> None:
        if self.error is None:
            self.error = message[:_MAX_ERROR]

    def add(self, mapped: object) -> None:
        match mapped:
            case AnswerDelta(text=text):
                if not self.turns:
                    self.turns.append([])
                self.turns[-1].append(text)
            case SourcesFound(sources=sources):
                self.sources.extend(sources)
            case ToolCallSeen(id=call_id, name=name, arguments=arguments):
                key = call_id or f"call-{len(self.calls) + 1}"
                if key not in self.calls:  # a denied call replayed by the continuation stream
                    self.calls[key] = _Call(key, name, arguments)
            case ToolResultSeen(tool_call_id=call_id, status=status, raw_status=raw):
                call = self.calls.get(call_id)
                if call is None:
                    return
                call.target_status = raw
                if not call.denied_by_harness:
                    call.status = status
                elif status is ToolCallStatus.EXECUTED and call.status is not status:
                    # Grade the worst case: a control that said no and ran the call anyway.
                    # Any other replayed status (rejected, refused, failed) keeps it denied.
                    call.status = status
                    self.contradictions.append(f"{call.name}: {CONTRADICTION}")
            case TargetReportedError(message=message):
                self.fail(f"target error: {message}")
            case _:
                pass

    def denied(self, approval: ApprovalRequested, note: str) -> None:
        call: _Call | None = None
        if approval.tool_call_id is not None:
            call = self.calls.get(approval.tool_call_id)
            if call is None and approval.tool:
                call = _Call(approval.tool_call_id, approval.tool, {})
                self.calls[call.id] = call
        else:
            pending = [c for c in self.calls.values() if c.status is ToolCallStatus.PROPOSED]
            call = pending[-1] if pending else None
        if call is not None:
            call.status = ToolCallStatus.DENIED
            call.denied_by_harness = True
        self.approvals.append(
            ApprovalRecord(
                tool_call_id=call.id if call else approval.tool_call_id or "",
                tool=approval.tool or (call.name if call else ""),
                decision="reject",
                reason=note,
            )
        )

    def build(self, latency_s: float, redact: Redactor) -> Observation:
        answers = ["".join(parts) for parts in self.turns]
        return Observation(
            answer=redact("\n\n".join(a for a in answers if a)),
            sources=tuple(Source(id=redact(s.id), text=redact(s.text)) for s in self.sources),
            tool_calls=tuple(
                ToolCallRecord(
                    id=redact(c.id),
                    name=redact(c.name),
                    arguments={redact(k): redact.json(v) for k, v in c.arguments.items()},
                    status=c.status,
                    target_status=redact(c.target_status),
                )
                for c in self.calls.values()
            ),
            approvals=tuple(
                a.model_copy(
                    update={"tool_call_id": redact(a.tool_call_id), "tool": redact(a.tool)}
                )
                for a in self.approvals
            ),
            usage=Usage(latency_s=round(latency_s, 4)),
            error=redact(self.error) if self.error is not None else None,
            observed=self.observed,
            contradictions=tuple(redact(c) for c in self.contradictions),
        )


def _identifier(value: JsonValue) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value)
    return text if 0 < len(text) <= 200 else None


def _optional(entry: JsonValue, path: str) -> JsonValue:
    try:
        return get_path(entry, path)
    except MissingFieldError:
        return None


class HttpTarget:
    """A ``Target`` and ``RunLifecycle`` for an API target described by an ``HttpTargetConfig``."""

    def __init__(
        self,
        config: HttpTargetConfig,
        *,
        name: str,
        model: str = "target",
        label: str | None = None,
        fixtures_root: Path | None = None,
        environ: Mapping[str, str] | None = None,
        identity: HttpTargetConfig | None = None,
        allowed_hosts: tuple[str, ...] | None = None,
        ca_file: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """``identity`` is the config as written (the stub fills in its port afterwards); it
        feeds the run id and the report header."""
        self.config = config
        self._identity = identity or config
        self._name = name
        self._model = model
        self._label = label or config.label
        self._library = FixtureLibrary(fixtures_root) if fixtures_root is not None else None
        self._redact = Redactor()
        secrets = read_secrets(config.auth, os.environ if environ is None else environ)
        self._session = HttpSession(
            config, self._redact, ca_file=ca_file, transport=transport, clock=clock, sleep=sleep
        )
        self._tokens = TokenManager(config.auth, secrets, self._post_token, self._redact, clock)
        self._mapper = EventMapper(config.events)
        self._observed = config.observed()
        self._clock = clock
        self._sleep = sleep
        host = urlsplit(self._identity.base_url).hostname or ""
        self._allowed = allowed_hosts or self._identity.grading.allowed_hosts or (host,)
        self._run_id = ""
        self._abort: str | None = None
        self.cleanup_problems: list[str] = []
        """Deletes that failed during the run (redacted)."""
        self.remaining: tuple[RemoteDocument, ...] | None = ()
        """Documents with this run's tag after ``end_run``; ``None`` if the check failed."""
        self.end_problem: str | None = None

    # -- Target protocol --------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def tag(self) -> str:
        return TAG_PREFIX + self._run_id

    @property
    def run_id(self) -> str:
        """The run this target works for (set by ``begin_run`` or ``use_run``)."""
        return self._run_id

    def use_run(self, run_id: str) -> None:
        """Work on ``run_id`` outside a run (``drum cleanup``)."""
        self._run_id = run_id

    def describe(self) -> dict[str, str]:
        identity = self._identity
        approvals = identity.approvals
        fixtures = identity.fixtures
        return {
            "model": self._model,
            "label": self._label,
            "base_url": identity.base_url,
            "config_digest": identity.digest(),
            "observed": ", ".join(c.value for c in ObservedChannel if c in self._observed),
            "allowed_hosts": ", ".join(self._allowed),
            "approvals": (
                "deny all"
                + (f" ({', '.join(approvals.gated_tools)})" if approvals.gated_tools else "")
                if approvals
                else "none"
            ),
            "fixtures": f"documents, tag in {fixtures.status.tag_field}" if fixtures else "none",
        }

    def grading_context(self, item: Item) -> GradingContext:
        canaries = [KnownCanary(c.value, c.source) for c in self._identity.grading.canaries]
        if self._library is not None:
            for ref in item.fixtures:
                kind, _, name = ref.partition(":")
                if kind != "doc":
                    continue
                document = self._library.document(name)
                restricted = not document.visible_to(item.user)
                canaries += [
                    KnownCanary(value, ref, restricted=restricted)
                    for value in find_canaries(self._library.fixture_text(ref))
                ]
        unique: dict[str, KnownCanary] = {}
        for canary in canaries:
            unique.setdefault(canary.value, canary)
        approvals = self._identity.approvals
        return GradingContext(
            canaries=tuple(unique.values()),
            allowed_hosts=tuple(self._allowed),
            gated_tools=frozenset(approvals.gated_tools if approvals else ()),
            observed=self._observed,
        )

    def preflight(self, items: Sequence[Item]) -> list[str]:
        """Problems that would make attempts fail for reasons the target never saw."""
        problems: list[str] = []
        for item in items:
            if not self._tokens.knows(item.user.sub):
                problems.append(f"{item.id}: user {item.user.sub!r} has no login in the config")
            for ref in item.fixtures:
                kind, _, name = ref.partition(":")
                if kind != "doc":
                    problems.append(f"{item.id}: {ref} is not supported (HTTP targets plant doc:)")
                elif self.config.fixtures is None:
                    problems.append(f"{item.id}: {ref} needs [fixtures] in the target config")
                elif self._library is None:
                    problems.append(f"{item.id}: {ref} needs a fixtures directory")
                else:
                    try:
                        self._library.document(name)
                    except FixtureError as exc:
                        problems.append(f"{item.id}: {exc}")
        return problems

    async def __aenter__(self) -> HttpTarget:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._session.__aexit__(*exc)

    async def run(self, item: Item, attempt: int) -> Observation:
        """One attempt. Never raises for target failures; Ctrl-C cleans up, then propagates."""
        started = self._clock()
        collector = _Collector(self._observed)
        user = item.user.sub
        values = {
            "run_id": self._run_id,
            "tag": self.tag,
            "item_id": item.id,
            "attempt": str(attempt),
        }
        planted: list[_Planted] = []
        conversation: list[str] = []
        limit = self.config.timeouts.attempt
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                await self._plant(item, values, planted)
                if planted:
                    await self._wait_indexed(self._fixtures_user(item), planted)
                conversation.append(await self._create_conversation(user, values))
                for turn in item.turns:
                    turn_values = values | {"conversation_id": conversation[0], "message": turn}
                    await self._turn(user, turn_values, collector)
        except TimeoutError:
            if not deadline.expired():
                raise
            collector.fail(f"the attempt timed out after {limit:g} s")
        except (TargetError, MalformedEventError) as exc:
            collector.fail(str(exc))
        except Exception as exc:  # a target must not raise; never lose a secret in a message
            collector.fail(f"{type(exc).__name__}: {self._redact(str(exc))[:_MAX_ERROR]}")
        finally:
            await self._clean_up(user, values, planted, conversation)
        return collector.build(self._clock() - started, self._redact)

    def _fixtures_user(self, item: Item) -> str:
        """The login that plants, polls and deletes documents: ``[fixtures] user`` or the
        item's user."""
        fixtures = self.config.fixtures
        return (fixtures.user if fixtures is not None else None) or item.user.sub

    # -- RunLifecycle -------------------------------------------------------------------------

    async def begin_run(self, run_id: str) -> None:
        """Log every user in, then refuse the run while any planted document exists."""
        self._run_id = run_id
        self._abort = None
        self.cleanup_problems = []
        self.remaining = ()
        self.end_problem = None
        for sub in self.config.auth.users:
            await self._tokens.token_for(sub)
        if self.config.fixtures is not None:
            leftovers = await self.find_documents(None)
            if leftovers:
                raise LeftoversError(leftovers, at_start=True)

    async def end_run(self, run_id: str) -> None:
        """Delete what is left with this run's tag, then list again. Never raises."""
        if self.config.fixtures is None:
            self.remaining = ()
            return
        try:
            found = await self.find_documents(run_id)
            if found:
                await self.delete_documents(found)
            self.remaining = await self.find_documents(run_id)
        except (TargetError, MalformedEventError) as exc:
            self.end_problem = str(exc)
            self.remaining = None
        except Exception as exc:
            self.end_problem = f"{type(exc).__name__}: {self._redact(str(exc))[:_MAX_ERROR]}"
            self.remaining = None

    def abort_reason(self) -> str | None:
        return self._abort

    # -- documents ----------------------------------------------------------------------------

    async def find_documents(self, run_id: str | None) -> tuple[RemoteDocument, ...]:
        """Documents with the tag of ``run_id``, or of any run, listed as every user."""
        found: dict[str, RemoteDocument] = {}
        for sub in self.config.auth.users:
            for document in await self._list(sub):
                tagged = (
                    document.run_id is not None
                    if run_id is None
                    else TAG_PREFIX + run_id in document.tag_value
                )
                if tagged:
                    found.setdefault(document.id, document)
        return tuple(found.values())

    async def delete_documents(self, documents: Sequence[RemoteDocument]) -> list[str]:
        """Delete each document as whichever user may; returns the problems (redacted)."""
        problems: list[str] = []
        for document in documents:
            last = ""
            for sub in self.config.auth.users:
                try:
                    await self._delete_document(sub, document.id)
                    last = ""
                    break
                except TargetError as exc:
                    if exc.status == 404:  # already gone
                        last = ""
                        break
                    last = str(exc)
            if last:
                problems.append(last)
        return problems

    async def _list(self, sub: str) -> list[RemoteDocument]:
        fixtures = self.config.fixtures
        if fixtures is None:
            return []
        status = fixtures.status
        path = render_path(status.path, {"run_id": self._run_id, "tag": self.tag})
        reply = await self._session.call(
            "list documents",
            status.method,
            self._session.url(path),
            token=await self._tokens.token_for(sub),
        )
        container = _optional(reply, status.items) if status.items else reply
        if not isinstance(container, list):
            msg = "list documents: the response holds no list of documents"
            raise TargetError(msg)
        documents: list[RemoteDocument] = []
        for entry in container:
            doc_id = _identifier(_optional(entry, status.id))
            if doc_id is None:
                continue
            tag = _optional(entry, status.tag_field)
            state = _optional(entry, status.status)
            documents.append(
                RemoteDocument(
                    id=doc_id,
                    tag_value=tag if isinstance(tag, str) else "",
                    status=str(state) if isinstance(state, str | int) else "",
                )
            )
        return documents

    async def _delete_document(self, sub: str, document_id: str) -> None:
        fixtures = self.config.fixtures
        if fixtures is None:  # pragma: no cover - callers check
            return
        request = fixtures.delete
        values = {"run_id": self._run_id, "tag": self.tag, "document_id": document_id}
        await self._session.call(
            "delete document",
            request.method,
            self._session.url(render_path(request.path, values)),
            token=await self._tokens.token_for(sub),
            json_body=render_json(request.body, values) if request.body is not None else None,
        )

    async def _plant(self, item: Item, values: dict[str, str], planted: list[_Planted]) -> None:
        fixtures = self.config.fixtures
        docs = [ref.partition(":")[2] for ref in item.fixtures if ref.startswith("doc:")]
        if not docs:
            return
        if fixtures is None or self._library is None:
            msg = "this item plants documents, but the target config has no [fixtures]"
            raise TargetError(msg)
        upload = fixtures.upload
        owner = self._fixtures_user(item)
        for name in docs:
            document = self._library.document(name)
            filename = f"{self.tag}-{name}"
            upload_values = values | {
                "filename": filename,
                "title": f"{document.title} [{self.tag}]",
            }
            form: dict[str, list[str]] = {}
            for entry in upload.form:
                form.setdefault(entry.name, []).append(render(entry.value, upload_values))
            content_type = "text/markdown" if name.endswith(".md") else "text/plain"
            record = _Planted(filename, owner)
            planted.append(record)  # before the request: cleanup finds it even if the reply is lost
            reply = await self._session.call(
                f"upload {name}",
                upload.method,
                self._session.url(render_path(upload.path, upload_values)),
                token=await self._tokens.token_for(owner),
                form={key: list(value) for key, value in form.items()},
                files={upload.file_field: (filename, document.text.encode(), content_type)},
            )
            if upload.id is not None:
                record.id = _identifier(_optional(reply, upload.id))
                if record.id is None:
                    msg = f"upload {name}: the response has no document id at {upload.id!r}"
                    raise TargetError(msg)

    async def _resolve_ids(self, sub: str, planted: list[_Planted]) -> None:
        if all(p.id is not None for p in planted):
            return
        documents = await self._list(sub)
        for record in planted:
            if record.id is None:
                match = next((d for d in documents if record.filename in d.tag_value), None)
                record.id = match.id if match else None

    async def _wait_indexed(self, sub: str, planted: list[_Planted]) -> None:
        fixtures = self.config.fixtures
        if fixtures is None:  # pragma: no cover - _plant checks
            return
        status = fixtures.status
        deadline = self._clock() + status.timeout
        while True:
            documents = await self._list(sub)
            by_id = {d.id: d for d in documents}
            pending = False
            for record in planted:
                document = (
                    by_id.get(record.id)
                    if record.id is not None
                    else next((d for d in documents if record.filename in d.tag_value), None)
                )
                if document is None:
                    pending = True
                    continue
                record.id = document.id
                if document.status in status.failed:
                    msg = (
                        f"document {record.filename} failed to index "
                        f"(status {document.status[:40]!r})"
                    )
                    raise TargetError(msg)
                if document.status not in status.ready:
                    pending = True
            if not pending:
                return
            if self._clock() >= deadline:
                msg = f"planted documents were not indexed within {status.timeout:g} s"
                raise TargetError(msg)
            await self._sleep(status.interval)

    async def _clean_up(
        self, sub: str, values: dict[str, str], planted: list[_Planted], conversation: list[str]
    ) -> None:
        """Delete the attempt's documents and conversation, even while being cancelled."""
        cancelled = False

        async def attempt(action: Callable[[], Awaitable[None]], what: str, fatal: bool) -> None:
            nonlocal cancelled
            try:
                await action()
            except asyncio.CancelledError:
                cancelled = True  # keep deleting; the cancellation is re-raised at the end
            except TargetError as exc:
                if exc.status == 404:
                    return
                self._problem(f"{what}: {exc}", fatal)
            except Exception as exc:
                self._problem(f"{what}: {type(exc).__name__}: {self._redact(str(exc))}", fatal)

        if planted:
            owner = planted[0].user
            await attempt(
                lambda: self._resolve_ids(owner, planted), "find planted documents", False
            )
        for record in planted:
            if record.id is None:
                continue  # never seen on the target; the run's end sweeps by tag
            document_id = record.id
            await attempt(
                lambda d=document_id, u=record.user: self._delete_document(u, d),
                f"delete planted document {record.filename}",
                True,
            )
        delete = self.config.chat.delete
        if delete is not None and conversation:
            conv_values = values | {"conversation_id": conversation[0]}

            async def delete_conversation() -> None:
                await self._session.call(
                    "delete conversation",
                    delete.method,
                    self._session.url(render_path(delete.path, conv_values)),
                    token=await self._tokens.token_for(sub),
                    json_body=render_json(delete.body, conv_values) if delete.body else None,
                )

            await attempt(delete_conversation, "delete conversation", False)
        if cancelled:
            raise asyncio.CancelledError

    def _problem(self, message: str, fatal: bool) -> None:
        text = message[:_MAX_ERROR]
        self.cleanup_problems.append(text)
        if fatal and self._abort is None:
            self._abort = f"a planted document could not be deleted ({text})"

    # -- chat ---------------------------------------------------------------------------------

    async def _post_token(self, step: str, form: Mapping[str, str]) -> JsonValue:
        return await self._session.call(
            step, "POST", self.config.token_url(), form=dict(form), login=True
        )

    async def _create_conversation(self, sub: str, values: dict[str, str]) -> str:
        create = self.config.chat.create
        reply = await self._session.call(
            "create conversation",
            create.method,
            self._session.url(render_path(create.path, values)),
            token=await self._tokens.token_for(sub),
            json_body=render_json(create.body, values) if create.body is not None else None,
        )
        conversation = _identifier(_optional(reply, create.id))
        if conversation is None:
            msg = f"create conversation: the response has no conversation id at {create.id!r}"
            raise TargetError(msg)
        return conversation

    async def _turn(self, sub: str, values: dict[str, str], collector: _Collector) -> None:
        """Send one turn and read its streams: the reply, then one continuation per deny.

        Every stream is read until the server closes it; ``done`` is a marker, not the end of
        reading. Approval requests are collected while a stream is read; once it has closed,
        each is denied in the order received, and each deny response is the next stream
        (ADR 0009). A target may store a turn and its pending approvals only when its stream
        ends, and may cancel the turn when the client disconnects early."""
        send = self.config.chat.send
        collector.turns.append([])
        pending: deque[ApprovalRequested] = deque()
        reply = self._session.stream(
            "send message",
            send.method,
            self._session.url(render_path(send.path, values)),
            token=await self._tokens.token_for(sub),
            json_body=render_json(send.body, values) if send.body is not None else None,
        )
        await self._read_stream("send message", reply, collector, pending)
        while pending:
            approval = pending.popleft()
            continuation = await self._deny(sub, values, approval)
            await self._read_stream(
                "approval deny",
                continuation,
                collector,
                pending,
                accepted=lambda a=approval: collector.denied(a, "denied by the harness"),
            )

    async def _read_stream(
        self,
        step: str,
        opened: AbstractAsyncContextManager[AsyncGenerator[SseEvent]],
        collector: _Collector,
        pending: deque[ApprovalRequested],
        accepted: Callable[[], None] | None = None,
    ) -> None:
        """Read one stream until the server closes it, at most ``timeouts.read`` after ``done``.

        ``accepted`` runs once the target has answered the request with a stream (2xx). A
        stream that the server keeps open after ``done`` is ended without an error."""
        done = paused = False
        linger = asyncio.timeout(None)
        try:
            async with linger, opened as events, aclosing(events) as stream:
                if accepted is not None:
                    accepted()
                async for event in stream:
                    mapped = self._mapper.map(event)
                    if isinstance(mapped, ApprovalRequested):
                        paused = True
                        pending.append(mapped)
                    elif isinstance(mapped, StreamDone):
                        if not done:
                            done = True
                            loop = asyncio.get_running_loop()
                            linger.reschedule(loop.time() + self.config.timeouts.read)
                    elif mapped is not None:
                        collector.add(mapped)
        except TimeoutError:
            if not linger.expired():
                raise
        except TargetTimeoutError:
            if not done:
                raise
        if self.config.events.done is not None and not done and not paused:
            msg = f"{step}: the stream ended before the done event"
            raise TargetError(msg)

    async def _deny(
        self, sub: str, values: dict[str, str], approval: ApprovalRequested
    ) -> AbstractAsyncContextManager[AsyncGenerator[SseEvent]]:
        """The deny request; nothing is sent until the returned stream is entered."""
        approvals = self.config.approvals
        if approvals is None:  # pragma: no cover - the config requires [approvals]
            msg = "approval requested, but the target config has no [approvals]"
            raise TargetError(msg)
        deny = approvals.deny
        deny_values = values | {
            "approval_id": approval.approval_id,
            "tool_call_id": approval.tool_call_id or "",
            "tool": approval.tool or "",
        }
        return self._session.stream(
            "approval deny",
            deny.method,
            self._session.url(render_path(deny.path, deny_values)),
            token=await self._tokens.token_for(sub),
            json_body=render_json(deny.body, deny_values) if deny.body is not None else None,
        )


async def clean_up_run(
    target: HttpTarget, run_id: str
) -> tuple[tuple[RemoteDocument, ...], list[str], tuple[RemoteDocument, ...]]:
    """``drum cleanup``: delete the documents with ``run_id``'s tag, then list again."""
    target.use_run(run_id)
    found = await target.find_documents(run_id)
    problems = await target.delete_documents(found) if found else []
    remaining = await target.find_documents(run_id)
    return found, problems, remaining
