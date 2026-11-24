"""The stand-in server: a small API target on ``127.0.0.1`` for tests and ``--target stub`` (M2).

It speaks the API shape of ``docs/examples/http-target.toml`` with generic names: a token
endpoint (password and refresh grants), conversations, messages answered with a server-sent
events stream, approvals whose decision response is the stream that continues the turn, and
document upload (multipart, with a required static field), listing (every user's documents in one
list, tagged by file name) and deletion.

Streams behave like a target that relays events as they arrive: a stream that asks for approval
still ends with ``done``, and the pending approval is stored only after the stream's last event
plus ``close_delay``, right before the server closes the response. A deny that arrives earlier
gets 404. A client that disconnects before the close cancels the turn: its approvals are never
stored.

Like the fake models it is driven by an item's ``self_test``: in obedient mode it replays the
item's tool calls (read tools emit a tool call, its result and every ready document as sources,
whoever uploaded it; write tools ask for approval) and gives the item's answer; in safe mode it
makes the same read calls, never a write call, and answers neutrally. It finds the item by the
user and the message text. Switches in ``StubOptions`` let tests force faults.

The stub serves the harness's own tests; nothing here runs for a real target.
"""

from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

from pecker_drum.items.schema import Item
from pecker_drum.twin.models import OBEDIENT_DEFAULT_ANSWER, SAFE_ANSWER
from pecker_drum.twin.tools import WRITE_TOOLS

GATED_TOOLS = WRITE_TOOLS
SOURCE_TOOLS = frozenset({"kb_search", "kb_get_document"})
REQUIRED_MODEL_FIELD = "model"
REQUIRED_UPLOAD_FIELD = "collection"
MAX_BODY = 8 * 1024 * 1024
NO_SCRIPT_ANSWER = "The stand-in has no script for this message."


_Events = list[tuple[str, dict[str, Any]]]


class StubMode(StrEnum):
    OBEDIENT = "fake:obedient"
    SAFE = "fake:safe"


@dataclass
class StubOptions:
    token_lifetime: float = 300.0
    refresh_lifetime: float = 1800.0
    polls_until_ready: int = 1
    """List requests a new document stays ``processing`` for."""
    never_ready: bool = False
    fail_indexing: bool = False
    ignore_deletes: bool = False
    """Deletes answer 204 but keep the document (leftover detection)."""
    replay_denied: bool = False
    """The continuation after a deny starts by replaying the denied call and its result."""
    denied_result_status: str = "rejected"
    """The tool result status the continuation sends for a rejected call; ``ok`` simulates a
    target that runs a denied call anyway."""
    refused_tools: frozenset[str] = frozenset()
    """Tools the stub's own permission check refuses (tool result status ``forbidden``)."""
    failing_tools: frozenset[str] = frozenset()
    """Tools that fail without an approval step (tool result status ``timeout``, a status in
    none of the config's lists)."""
    uploaders: frozenset[str] | None = None
    """Users who may upload documents (others get 403); ``None``: everyone. A document can
    always be deleted only by its owner."""
    message_status: int | None = None
    """Answer every message with this HTTP status instead of a stream."""
    chunk_size: int | None = None
    """Write streams in pieces of this many bytes (splits lines and UTF-8 characters)."""
    crlf: bool = False
    leaky: bool = False
    """Echo the bearer token, the ID token and the user's password in answers, sources and
    error bodies."""
    stall: float = 0.0
    """Seconds to wait inside a message stream before the answer."""
    close_delay: float = 0.02
    """Seconds between a stream's last event and its close. Pending approvals are stored at the
    end of it, so a deny sent before the stream closed gets 404."""
    on_stream: Callable[[], None] | None = None
    """Called when a message stream starts (tests use it to simulate Ctrl-C)."""


@dataclass
class _Document:
    id: str
    owner: str
    filename: str
    text: str
    status: str = "processing"
    polls: int = 0


@dataclass
class _Turn:
    username: str
    conversation: str
    steps: list[tuple[str, str, dict[str, Any]]]
    """``(kind, tool or answer, arguments)``; kind is read, gated, refused, failing or answer."""
    position: int = 0
    calls: int = 0


@dataclass
class _Pending:
    turn: _Turn
    call_id: str
    tool: str
    arguments: dict[str, Any]


@dataclass
class StubState:
    users: dict[str, str]
    access: dict[str, tuple[str, float]] = field(default_factory=dict[str, tuple[str, float]])
    refresh: dict[str, tuple[str, float]] = field(default_factory=dict[str, tuple[str, float]])
    issued: list[str] = field(default_factory=list[str])
    """Every access, refresh and ID token handed out (leak tests)."""
    id_tokens: dict[str, str] = field(default_factory=dict[str, str])
    """The latest ID token per user (a password grant with scope ``openid``)."""
    grants: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """``(grant type, user)`` per token request."""
    documents: dict[str, _Document] = field(default_factory=dict[str, _Document])
    deleted: list[str] = field(default_factory=list[str])
    deleted_by: dict[str, str] = field(default_factory=dict[str, str])
    """Document id to the user who deleted it."""
    conversations: dict[str, str] = field(default_factory=dict[str, str])
    deleted_conversations: list[str] = field(default_factory=list[str])
    pending: dict[str, _Pending] = field(default_factory=dict[str, _Pending])
    disconnects: int = 0
    """Streams the client closed before the server did (their turns were cancelled)."""
    decisions: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    """``(approval id, decision, note)`` per decision request."""
    uploads: list[dict[str, list[str]]] = field(default_factory=list[dict[str, list[str]]])
    """The form fields of every upload."""
    bodies: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    """``(path, JSON body)`` of every JSON request."""
    requests: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])


def parse_multipart(
    content_type: str, body: bytes
) -> tuple[dict[str, list[str]], list[tuple[str, str, bytes]]]:
    """Form fields and files of a ``multipart/form-data`` body (standard library only)."""
    raw = b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    message = BytesParser(policy=policy.HTTP).parsebytes(raw)
    fields: dict[str, list[str]] = {}
    files: list[tuple[str, str, bytes]] = []
    for part in message.iter_parts():
        name = str(part.get_param("name", header="content-disposition") or "")
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        filename = part.get_filename()
        if filename is not None:
            files.append((name, filename, data))
        else:
            fields.setdefault(name, []).append(data.decode("utf-8", errors="replace"))
    return fields, files


class StubServer:
    """Runs in a daemon thread; ``with StubServer(...) as server: server.url``."""

    def __init__(
        self,
        items: Sequence[Item],
        mode: StubMode,
        users: Mapping[str, str],
        options: StubOptions | None = None,
    ) -> None:
        self.mode = mode
        self.options = options or StubOptions()
        self.state = StubState(users=dict(users))
        self._items = {(item.user.sub, turn): item for item in items for turn in item.turns}
        self.lock = threading.Lock()
        """Guards ``state``; the handler threads take it."""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._httpd is None:
            msg = "the stand-in server is not running"
            raise RuntimeError(msg)
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> StubServer:
        handler = type("StubHandler", (_Handler,), {"stub": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> StubServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- state, called by the handler under the lock -------------------------------------------

    def issue(self, username: str, grant: str, scope: str = "") -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = time.monotonic()
        self.state.access[access] = (username, now + self.options.token_lifetime)
        self.state.refresh[refresh] = (username, now + self.options.refresh_lifetime)
        self.state.issued += [access, refresh]
        self.state.grants.append((grant, username))
        reply: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.options.token_lifetime,
            "refresh_token": refresh,
            "refresh_expires_in": self.options.refresh_lifetime,
        }
        if "openid" in scope.split():
            id_token = "id." + secrets.token_urlsafe(24)
            self.state.id_tokens[username] = id_token
            self.state.issued.append(id_token)
            reply["id_token"] = id_token
        return reply

    def user_for(self, header: str | None) -> tuple[str, str] | None:
        if not header or not header.startswith("Bearer "):
            return None
        token = header.removeprefix("Bearer ")
        entry = self.state.access.get(token)
        if entry is None or entry[1] < time.monotonic():
            return None
        return entry[0], token

    def script(self, username: str, conversation: str, content: str) -> _Turn:
        item = self._items.get((username, content))
        if item is None:
            return _Turn(username, conversation, [("answer", NO_SCRIPT_ANSWER, {})])
        steps: list[tuple[str, str, dict[str, Any]]] = []
        for call in item.self_test.tool_calls:
            if call.name in GATED_TOOLS and self.mode is StubMode.SAFE:
                continue
            if call.name in self.options.refused_tools:
                kind = "refused"
            elif call.name in self.options.failing_tools:
                kind = "failing"
            elif call.name in GATED_TOOLS:
                kind = "gated"
            else:
                kind = "read"
            steps.append((kind, call.name, dict(call.args)))
        if self.mode is StubMode.OBEDIENT:
            answer = item.self_test.answer or OBEDIENT_DEFAULT_ANSWER
        else:
            answer = SAFE_ANSWER
        steps.append(("answer", answer, {}))
        return _Turn(username, conversation, steps)

    def play(self, turn: _Turn, token: str) -> tuple[_Events, dict[str, _Pending]]:
        """Events from the turn's position until it ends or pauses for an approval, and the
        approvals to store when the stream closes."""
        events: _Events = [("ping", {})]
        while turn.position < len(turn.steps):
            kind, name, arguments = turn.steps[turn.position]
            turn.position += 1
            if kind == "answer":
                text = name
                if self.options.leaky:
                    password = self.state.users.get(turn.username, "")
                    id_token = self.state.id_tokens.get(turn.username, "")
                    text += f" (debug: token {token}, id {id_token}, password {password})"
                words = text.split(" ")
                events += [
                    ("token", {"delta": word + (" " if i < len(words) - 1 else "")})
                    for i, word in enumerate(words)
                ]
                events.append(("done", {}))
                return events, {}
            turn.calls += 1
            call_id = f"call-{turn.calls}"
            call_args = dict(arguments)
            if self.options.leaky:
                call_args["debug"] = self.state.users.get(turn.username, "")
            events.append(
                ("tool_call", {"id": call_id, "name": name, "arguments": json.dumps(call_args)})
            )
            if kind == "gated":
                approval = f"appr-{secrets.token_hex(6)}"
                events.append(
                    (
                        "approval_required",
                        {"approval_id": approval, "tool_call_id": call_id, "tool": name},
                    )
                )
                events.append(("done", {}))  # the decision response continues the turn
                return events, {approval: _Pending(turn, call_id, name, call_args)}
            if kind in ("refused", "failing"):
                status = "forbidden" if kind == "refused" else "timeout"
                events.append(("tool_result", {"tool_call_id": call_id, "status": status}))
                continue
            events.append(
                ("tool_result", {"tool_call_id": call_id, "status": "ok", "output": "done"})
            )
            if name in SOURCE_TOOLS:
                documents = [
                    {"id": d.id, "title": d.filename, "text": d.text}
                    for d in self.state.documents.values()
                    if d.status == "ready"  # a shared knowledge base: every owner's documents
                ]
                if self.options.leaky:
                    documents.append({"id": "debug", "title": "debug", "text": f"token {token}"})
                events.append(("sources", {"documents": documents}))
        events.append(("done", {}))
        return events, {}

    def resume(
        self, approval: str, decision: str, token: str
    ) -> tuple[_Events, dict[str, _Pending]] | None:
        pending = self.state.pending.pop(approval, None)
        if pending is None:
            return None
        events: _Events = []
        if self.options.replay_denied:
            events.append(
                (
                    "tool_call",
                    {
                        "id": pending.call_id,
                        "name": pending.tool,
                        "arguments": json.dumps(pending.arguments),
                    },
                )
            )
        status = "ok" if decision == "approve" else self.options.denied_result_status
        events.append(("tool_result", {"tool_call_id": pending.call_id, "status": status}))
        more, approvals = self.play(pending.turn, token)
        return events + more, approvals

    def list_documents(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = []
        for document in self.state.documents.values():
            if document.status == "processing":
                document.polls += 1
                if self.options.fail_indexing:
                    document.status = "failed"
                elif (
                    not self.options.never_ready and document.polls > self.options.polls_until_ready
                ):
                    document.status = "ready"
            listed.append(
                {
                    "id": document.id,
                    "filename": document.filename,
                    "status": document.status,
                    "owner": document.owner,
                }
            )
        return listed


class _Handler(BaseHTTPRequestHandler):
    stub: StubServer
    server_version = "pecker-stub/1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    # -- plumbing ---------------------------------------------------------------------------

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, MAX_BODY)) if length > 0 else b""

    def _send(self, status: int, payload: object | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self._log(status)

    def _log(self, status: int) -> None:
        with self.stub.lock:
            self.stub.state.requests.append((self.command, urlsplit(self.path).path, status))

    def _stream(self, events: _Events, approvals: dict[str, _Pending]) -> None:
        """Write the events, wait ``close_delay``, store the approvals, then close (the handler
        returns). A client that disconnected before that cancels the turn."""
        options = self.stub.options
        newline = "\r\n" if options.crlf else "\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self._log(HTTPStatus.OK)
        text = f": stand-in stream{newline}{newline}"
        for name, data in events:
            data_text = json.dumps(data, ensure_ascii=False)
            text += f"event: {name}{newline}data: {data_text}{newline}{newline}"
        payload = text.encode()
        if options.stall:
            split = payload.find(b"event: token")
            head, tail = (payload, b"") if split < 0 else (payload[:split], payload[split:])
            self.wfile.write(head)
            self.wfile.flush()
            time.sleep(options.stall)
            payload = tail
        size = options.chunk_size or len(payload) or 1
        try:
            for start in range(0, len(payload), size):
                self.wfile.write(payload[start : start + size])
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._disconnected()
            return
        if self._client_left(options.close_delay):
            self._disconnected()
            return
        with self.stub.lock:
            self.stub.state.pending.update(approvals)

    def _client_left(self, wait: float) -> bool:
        """Wait up to ``wait`` seconds; True if the client closed the connection meanwhile."""
        try:
            readable, _, _ = select.select([self.connection], [], [], wait)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _disconnected(self) -> None:
        with self.stub.lock:
            self.stub.state.disconnects += 1

    def _json_body(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self._body() or b"{}")
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        body = cast("dict[str, Any]", value)
        with self.stub.lock:
            self.stub.state.bodies.append((urlsplit(self.path).path, body))
        return body

    def _user(self) -> tuple[str, str] | None:
        with self.stub.lock:
            found = self.stub.user_for(self.headers.get("Authorization"))
        if found is None:
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid_token"})
        return found

    def _parts(self) -> list[str]:
        return [part for part in urlsplit(self.path).path.split("/") if part]

    # -- routes -----------------------------------------------------------------------------

    def do_POST(self) -> None:
        parts = self._parts()
        if parts == ["oauth", "token"]:
            self._token()
        elif parts == ["api", "conversations"]:
            self._create_conversation()
        elif len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "messages":
            self._message(parts[2])
        elif len(parts) == 4 and parts[:2] == ["api", "approvals"] and parts[3] == "decision":
            self._decision(parts[2])
        elif parts == ["api", "documents"]:
            self._upload()
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_GET(self) -> None:
        if self._parts() == ["api", "documents"]:
            if self._user() is None:
                return
            with self.stub.lock:
                listed = self.stub.list_documents()
            self._send(HTTPStatus.OK, {"documents": listed})
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_DELETE(self) -> None:
        parts = self._parts()
        found = self._user()
        if found is None:
            return
        username = found[0]
        stub = self.stub
        if len(parts) == 3 and parts[:2] == ["api", "documents"]:
            with stub.lock:
                document = stub.state.documents.get(parts[2])
                if document is None:
                    status = HTTPStatus.NOT_FOUND
                elif document.owner != username:
                    status = HTTPStatus.FORBIDDEN
                else:
                    status = HTTPStatus.NO_CONTENT
                    if not stub.options.ignore_deletes:
                        del stub.state.documents[parts[2]]
                        stub.state.deleted.append(parts[2])
                        stub.state.deleted_by[parts[2]] = username
            self._send(status)
        elif len(parts) == 3 and parts[:2] == ["api", "conversations"]:
            with stub.lock:
                owner = stub.state.conversations.get(parts[2])
                if owner == username:
                    del stub.state.conversations[parts[2]]
                    stub.state.deleted_conversations.append(parts[2])
            self._send(HTTPStatus.NO_CONTENT if owner == username else HTTPStatus.NOT_FOUND)
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _token(self) -> None:
        form = dict(parse_qsl(self._body().decode("utf-8", errors="replace")))
        stub = self.stub
        with stub.lock:
            if not form.get("client_id"):
                reply: tuple[int, dict[str, Any]] = (
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "invalid_client"},
                )
            elif form.get("grant_type") == "password":
                username = form.get("username", "")
                if username in stub.state.users and stub.state.users[username] == form.get(
                    "password"
                ):
                    reply = (HTTPStatus.OK, stub.issue(username, "password", form.get("scope", "")))
                else:
                    reply = (HTTPStatus.UNAUTHORIZED, {"error": "invalid_grant"})
            elif form.get("grant_type") == "refresh_token":
                entry = stub.state.refresh.pop(form.get("refresh_token", ""), None)
                if entry is None or entry[1] < time.monotonic():
                    reply = (HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                else:
                    reply = (HTTPStatus.OK, stub.issue(entry[0], "refresh_token"))
            else:
                reply = (HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"})
        self._send(*reply)

    def _create_conversation(self) -> None:
        found = self._user()
        if found is None:
            return
        body = self._json_body()
        if body is None or not isinstance(body.get(REQUIRED_MODEL_FIELD), str):
            self._send(HTTPStatus.BAD_REQUEST, {"error": f"{REQUIRED_MODEL_FIELD} is required"})
            return
        conversation = f"conv-{secrets.token_hex(6)}"
        with self.stub.lock:
            self.stub.state.conversations[conversation] = found[0]
        self._send(HTTPStatus.CREATED, {"id": conversation})

    def _message(self, conversation: str) -> None:
        found = self._user()
        if found is None:
            return
        username, token = found
        stub = self.stub
        body = self._json_body()
        if stub.options.message_status is not None:
            detail = f"rate limited; token {token}" if stub.options.leaky else "rate limited"
            self._send(stub.options.message_status, {"error": detail})
            return
        with stub.lock:
            owner = stub.state.conversations.get(conversation)
        if owner != username:
            self._send(HTTPStatus.NOT_FOUND, {"error": "no such conversation"})
            return
        content = body.get("content") if body else None
        if (
            body is None
            or not isinstance(content, str)
            or not isinstance(body.get(REQUIRED_MODEL_FIELD), str)
        ):
            self._send(HTTPStatus.BAD_REQUEST, {"error": "content and model are required"})
            return
        if stub.options.on_stream is not None:
            stub.options.on_stream()
        with stub.lock:
            events, approvals = stub.play(stub.script(username, conversation, content), token)
        self._stream(events, approvals)

    def _decision(self, approval: str) -> None:
        found = self._user()
        if found is None:
            return
        body = self._json_body() or {}
        decision = str(body.get("decision", ""))
        if decision not in ("reject", "approve"):
            self._send(HTTPStatus.BAD_REQUEST, {"error": "decision must be reject or approve"})
            return
        stub = self.stub
        with stub.lock:
            stub.state.decisions.append((approval, decision, str(body.get("note", ""))))
            resumed = stub.resume(approval, decision, found[1])
        if resumed is None:
            self._send(HTTPStatus.NOT_FOUND, {"error": "no such approval"})
            return
        self._stream(*resumed)

    def _upload(self) -> None:
        found = self._user()
        if found is None:
            return
        body = self._body()  # read it first: answering early would reset the connection
        uploaders = self.stub.options.uploaders
        if uploaders is not None and found[0] not in uploaders:
            self._send(HTTPStatus.FORBIDDEN, {"error": "only editors may upload"})
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "multipart/form-data only"})
            return
        fields, files = parse_multipart(content_type, body)
        if not fields.get(REQUIRED_UPLOAD_FIELD) or len(files) != 1 or files[0][0] != "file":
            self._send(
                HTTPStatus.BAD_REQUEST, {"error": f"file and {REQUIRED_UPLOAD_FIELD} required"}
            )
            return
        _, filename, data = files[0]
        document = _Document(
            id=f"doc-{secrets.token_hex(6)}",
            owner=found[0],
            filename=filename,
            text=data.decode("utf-8", errors="replace"),
        )
        with self.stub.lock:
            self.stub.state.documents[document.id] = document
            self.stub.state.uploads.append(fields)
        self._send(HTTPStatus.CREATED, {"id": document.id, "status": document.status})
