"""The HTTP target config: a TOML file validated by a strict Pydantic schema (M2, ADR 0009).

Unknown keys are rejected at every level. A config names environment variables; it never holds a
secret value. Passwords and client secrets come from variables whose names start with ``DRUM_``
(and are not ``DRUM_OPENROUTER_MODEL``), so a config cannot send the OpenRouter key or any other
secret of the shell to a token endpoint. Plain ``http`` is accepted for loopback hosts only
(``localhost``, ``127.0.0.0/8``, ``::1``); everything else needs ``https``.

See ``docs/examples/http-target.toml`` for a commented example.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from pecker_drum.items.canary import is_canary
from pecker_drum.observation import ObservedChannel
from pecker_drum.targets.http import templates

RUN_VARS = frozenset({"run_id", "tag"})
ATTEMPT_VARS = RUN_VARS | {"item_id", "attempt"}
CREATE_VARS = ATTEMPT_VARS
SEND_VARS = ATTEMPT_VARS | {"conversation_id", "message"}
CHAT_DELETE_VARS = ATTEMPT_VARS | {"conversation_id"}
DENY_VARS = ATTEMPT_VARS | {"conversation_id", "approval_id", "tool_call_id", "tool"}
UPLOAD_VARS = ATTEMPT_VARS | {"filename", "title"}
STATUS_VARS = RUN_VARS
DOCUMENT_DELETE_VARS = RUN_VARS | {"document_id"}

FORBIDDEN_ENV_NAMES = frozenset({"DRUM_OPENROUTER_MODEL"})
_ENV_NAME = re.compile(r"DRUM_[A-Z0-9_]{1,60}")
_PATH = re.compile(r"[A-Za-z0-9_@$-]+(\.[A-Za-z0-9_@$-]+)*")
_HEADER_SAFE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


class ConfigError(ValueError):
    """The config file is missing, not TOML or not valid. The message never quotes a secret
    (a config holds none)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _env_name(value: str | None) -> str | None:
    if value is None:
        return value
    if not _ENV_NAME.fullmatch(value) or value in FORBIDDEN_ENV_NAMES:
        msg = (
            f"{value!r} is not allowed: secrets come from variables named DRUM_<NAME> "
            "(upper case, digits, underscores), never DRUM_OPENROUTER_MODEL"
        )
        raise ValueError(msg)
    return value


def _dotted(value: str | None) -> str | None:
    if value is not None and not _PATH.fullmatch(value):
        msg = f"{value!r} is not a dotted field path such as data.delta or items.0.id"
        raise ValueError(msg)
    return value


def is_loopback(host: str) -> bool:
    """``localhost`` (the literal name), ``127.0.0.0/8`` or ``::1``."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_url(url: str, what: str) -> str:
    """An absolute http(s) URL without credentials or fragment; http only for loopback hosts."""
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises for a bad port
    except ValueError as exc:
        msg = f"{what} is not a valid URL: {exc}"
        raise ValueError(msg) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        msg = f"{what} must be an absolute http or https URL"
        raise ValueError(msg)
    if parts.username is not None or parts.password is not None:
        msg = f"{what} must not contain a user name or password"
        raise ValueError(msg)
    if parts.fragment:
        msg = f"{what} must not contain a fragment"
        raise ValueError(msg)
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        msg = (
            f"{what} uses plain http for a host that is not loopback; use https "
            "(http is allowed for localhost, 127.0.0.0/8 and ::1 only)"
        )
        raise ValueError(msg)
    return url


DottedPath = Annotated[str, Field(min_length=1, max_length=200)]
Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class Request(_Strict):
    """One request: a method, a path below ``base_url`` and an optional JSON body template."""

    method: Method = "POST"
    path: str = Field(min_length=1, max_length=500)
    body: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _relative_path(self) -> Self:
        if not self.path.startswith("/") or self.path.startswith("//") or "#" in self.path:
            msg = f"path {self.path!r} must start with a single / and have no fragment"
            raise ValueError(msg)
        return self

    def check_vars(self, allowed: Collection[str], where: str) -> None:
        templates.check(self.path, allowed, f"{where}.path")
        for text in templates.json_strings(self.body or {}):
            templates.check(text, allowed, f"{where}.body")


class ChatCreate(Request):
    id: DottedPath
    """Where the new conversation's id sits in the response."""


class Chat(_Strict):
    create: ChatCreate
    send: Request
    """Its response is the event stream of the turn."""
    delete: Request | None = None


class _EventMap(_Strict):
    names: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(min_length=1)
    """SSE event names (``match = "event"``) or values of the type field (``match = "field"``)."""


class AnswerEvents(_EventMap):
    text: DottedPath


class SourceEvents(_EventMap):
    items: DottedPath | None = None
    """A list of sources in the event; without it the event data is one source."""
    id: DottedPath
    text: DottedPath


class ToolCallEvents(_EventMap):
    id: DottedPath | None = None
    name: DottedPath
    arguments: DottedPath | None = None
    """An object, or a string holding JSON."""


Status = Annotated[str, Field(min_length=1, max_length=100)]


class ToolResultEvents(_EventMap):
    tool_call_id: DottedPath
    status: DottedPath | None = None
    """Without a status field every tool result counts as executed."""
    executed: tuple[Status, ...] = ()
    """Status values that mean the tool ran; only these count for ``tool_executed``."""
    rejected: tuple[Status, ...] = ()
    """Status values that mean an approval rejected the call (``denied``)."""
    refused: tuple[Status, ...] = ()
    """Status values that mean the target's own permission check stopped the call."""

    @model_validator(mode="after")
    def _statuses(self) -> Self:
        lists = (self.executed, self.rejected, self.refused)
        if self.status is None:
            if any(lists):
                msg = "executed, rejected and refused need a status field"
                raise ValueError(msg)
            return self
        if not self.executed:
            msg = "with a status field, executed must name the values that mean the tool ran"
            raise ValueError(msg)
        values = [value for values in lists for value in values]
        if len(values) != len(set(values)):
            msg = "a status value may appear in only one of executed, rejected and refused"
            raise ValueError(msg)
        return self


class ApprovalEvents(_EventMap):
    id: DottedPath
    tool_call_id: DottedPath | None = None
    tool: DottedPath | None = None


class ErrorEvents(_EventMap):
    message: DottedPath | None = None


class DoneEvents(_EventMap):
    pass


class Events(_Strict):
    match: Literal["event", "field"] = "event"
    """Match on the SSE ``event:`` line or on a field of the JSON data."""
    type_field: DottedPath | None = None
    answer: AnswerEvents
    sources: SourceEvents | None = None
    tool_call: ToolCallEvents | None = None
    tool_result: ToolResultEvents | None = None
    approval: ApprovalEvents | None = None
    error: ErrorEvents | None = None
    done: DoneEvents | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.match == "field" and self.type_field is None:
            msg = 'match = "field" needs type_field'
            raise ValueError(msg)
        if self.match == "event" and self.type_field is not None:
            msg = 'type_field is only used with match = "field"'
            raise ValueError(msg)
        for path in self.paths():
            _dotted(path)
        seen: dict[str, str] = {}
        for kind, mapping in self.mappings().items():
            for name in mapping.names:
                if name in seen:
                    msg = f"event name {name!r} is mapped twice ({seen[name]} and {kind})"
                    raise ValueError(msg)
                seen[name] = kind
        return self

    def mappings(self) -> dict[str, _EventMap]:
        candidates: dict[str, _EventMap | None] = {
            "answer": self.answer,
            "sources": self.sources,
            "tool_call": self.tool_call,
            "tool_result": self.tool_result,
            "approval": self.approval,
            "error": self.error,
            "done": self.done,
        }
        return {kind: mapping for kind, mapping in candidates.items() if mapping is not None}

    def paths(self) -> list[str]:
        found: list[str] = [self.type_field] if self.type_field else []
        for mapping in self.mappings().values():
            for key, value in mapping.model_dump().items():
                if key != "names" and isinstance(value, str) and key not in _STATUS_LISTS:
                    found.append(value)
        return found


_STATUS_LISTS = frozenset({"executed", "rejected", "refused"})


class Approvals(_Strict):
    deny: Request
    """Its response is a new event stream that continues the turn."""
    gated_tools: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    """Tools behind an approval step; used only for the self-test's expected outcome."""


class FormField(_Strict):
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=1000)
    """A template; ``{filename}``, ``{title}``, ``{tag}`` and the attempt variables are allowed."""


class Upload(_Strict):
    method: Literal["POST", "PUT"] = "POST"
    path: str = Field(min_length=1, max_length=500)
    file_field: str = Field(default="file", min_length=1, max_length=100)
    form: tuple[FormField, ...] = ()
    """Extra form fields; a name may repeat."""
    id: DottedPath | None = None
    """Where the new document's id sits in the response. Without it the harness finds the
    document in the list by its tag field (the uploaded file name)."""

    @model_validator(mode="after")
    def _relative_path(self) -> Self:
        Request(method=self.method, path=self.path)
        return self


class DocumentList(_Strict):
    """The list endpoint: polled until planted documents are indexed, and searched for
    leftovers. It must return every document in one response (no pagination)."""

    method: Literal["GET"] = "GET"
    path: str = Field(min_length=1, max_length=500)
    items: DottedPath | None = None
    """Where the list sits in the response; without it the response is the list."""
    id: DottedPath
    status: DottedPath
    tag_field: DottedPath
    """The field that carries the tag ``drum-fixture-<run id>`` (the uploaded file name)."""
    ready: tuple[Status, ...] = Field(min_length=1)
    failed: tuple[Status, ...] = ()
    interval: float = Field(default=1.0, gt=0, le=60)
    timeout: float = Field(default=300.0, gt=0, le=3600)

    @model_validator(mode="after")
    def _relative_path(self) -> Self:
        Request(method=self.method, path=self.path)
        return self


class Fixtures(_Strict):
    user: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    """The login (a key of ``[auth.users]``) that uploads, polls and deletes an attempt's
    documents. Default: the item's user. Lets an editor plant while a viewer asks."""
    upload: Upload
    status: DocumentList
    delete: Request


class UserLogin(_Strict):
    username: str = Field(min_length=1, max_length=200)
    password_env: str
    """The name of the variable that holds the password (``DRUM_...``), never the password."""

    @model_validator(mode="after")
    def _env(self) -> Self:
        _env_name(self.password_env)
        return self


class OidcPasswordAuth(_Strict):
    kind: Literal["oidc_password"]
    token_url: str = Field(min_length=1, max_length=500)
    """Absolute (the issuer's host), or a path below the origin of ``base_url``."""
    client_id: str = Field(min_length=1, max_length=200)
    client_secret_env: str | None = None
    scope: str = Field(default="openid", max_length=500)
    refresh_margin: float = Field(default=30.0, ge=0, le=3600)
    """Refresh a token this many seconds before it expires (at most half its lifetime)."""
    users: dict[Annotated[str, Field(min_length=1, max_length=64)], UserLogin] = Field(min_length=1)
    """Keyed by ``item.user.sub``."""

    @model_validator(mode="after")
    def _env(self) -> Self:
        _env_name(self.client_secret_env)
        return self


class Timeouts(_Strict):
    connect: float = Field(default=10.0, gt=0, le=600)
    read: float = Field(default=60.0, gt=0, le=3600)
    """The request timeout: the longest wait for the next bytes of a response or stream."""
    attempt: float = Field(default=600.0, gt=0, le=86400)


class Limits(_Strict):
    requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    max_wall_time: float = Field(default=7200.0, gt=0)
    max_attempts: int = Field(default=1000, ge=1)
    max_consecutive_errors: int = Field(default=5, ge=1)
    max_response_bytes: int = Field(default=1_048_576, ge=1024)
    max_stream_bytes: int = Field(default=8_388_608, ge=1024)
    max_stream_events: int = Field(default=50_000, ge=1)


class TargetCanary(_Strict):
    value: str
    source: str = Field(default="target", min_length=1, max_length=100)

    @model_validator(mode="after")
    def _canary(self) -> Self:
        if not is_canary(self.value):
            msg = f"not a canary: {self.value!r}"
            raise ValueError(msg)
        return self


class Grading(_Strict):
    allowed_hosts: tuple[Annotated[str, Field(min_length=1)], ...] | None = None
    """Hosts links and images may point to; default: the host of ``base_url``."""
    canaries: tuple[TargetCanary, ...] = ()
    """Canaries the target holds itself (for example in its system prompt)."""


class HttpTargetConfig(_Strict):
    label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    """Names the target's model in report file names."""
    base_url: str = Field(min_length=1, max_length=500)
    ca_file: str | None = None
    """A CA bundle for a self-signed certificate, relative to the config file. TLS verification
    cannot be switched off."""
    timeouts: Timeouts = Timeouts()
    limits: Limits = Limits()
    auth: OidcPasswordAuth
    chat: Chat
    events: Events
    approvals: Approvals | None = None
    fixtures: Fixtures | None = None
    grading: Grading = Grading()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        check_url(self.base_url, "base_url")
        if urlsplit(self.base_url).query:
            msg = "base_url must not contain a query"
            raise ValueError(msg)
        check_url(self.token_url(), "auth.token_url")
        if (self.events.approval is None) != (self.approvals is None):
            msg = "events.approval and [approvals] go together: an approval must be denied"
            raise ValueError(msg)
        self.chat.create.check_vars(CREATE_VARS, "chat.create")
        self.chat.send.check_vars(SEND_VARS, "chat.send")
        if self.chat.delete is not None:
            self.chat.delete.check_vars(CHAT_DELETE_VARS, "chat.delete")
        if self.approvals is not None:
            self.approvals.deny.check_vars(DENY_VARS, "approvals.deny")
        if self.fixtures is not None:
            user = self.fixtures.user
            if user is not None and user not in self.auth.users:
                msg = f"fixtures.user {user!r} is not a key of [auth.users]"
                raise ValueError(msg)
            upload = self.fixtures.upload
            templates.check(upload.path, UPLOAD_VARS, "fixtures.upload.path")
            for field in upload.form:
                if not _HEADER_SAFE.fullmatch(field.name):
                    msg = f"fixtures.upload.form: {field.name!r} is not a plain field name"
                    raise ValueError(msg)
                templates.check(field.value, UPLOAD_VARS, f"fixtures.upload.form.{field.name}")
            templates.check(self.fixtures.status.path, STATUS_VARS, "fixtures.status.path")
            self.fixtures.delete.check_vars(DOCUMENT_DELETE_VARS, "fixtures.delete")
        return self

    def token_url(self) -> str:
        """The absolute token URL: as given, or a path below the origin of ``base_url``."""
        url = self.auth.token_url
        if url.startswith("/") and not url.startswith("//"):
            base = urlsplit(self.base_url)
            return f"{base.scheme}://{base.netloc}{url}"
        return url

    def observed(self) -> frozenset[ObservedChannel]:
        """The channels this target exposes, from the events it maps (ADR 0009)."""
        channels = {ObservedChannel.ANSWER}
        if self.events.sources is not None:
            channels.add(ObservedChannel.SOURCES)
        if self.events.tool_call is not None:
            channels.add(ObservedChannel.TOOL_CALLS)
        if self.events.tool_result is not None:
            channels.add(ObservedChannel.TOOL_RESULTS)
        if self.events.approval is not None and self.approvals is not None:
            channels.add(ObservedChannel.APPROVALS)
        return frozenset(channels)

    def digest(self) -> str:
        """A hash of the config for the run id. ``[limits]`` is left out, so a run stopped by a
        cap can resume with a larger one. The config names variables but holds no secret
        values, so none can enter the digest."""
        material = json.dumps(self.model_dump(mode="json", exclude={"limits"}), sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest()[:16]


def _describe(error: ValidationError) -> str:
    lines: list[str] = []
    for detail in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in detail["loc"]) or "(top level)"
        message = str(detail["msg"]).removeprefix("Value error, ")
        lines.append(f"{location}: {message}")
    return "; ".join(lines)


def parse_config(text: str, source: str = "config") -> HttpTargetConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{source} is not valid TOML: {exc}"
        raise ConfigError(msg) from None
    try:
        return HttpTargetConfig.model_validate(raw)
    except ValidationError as exc:
        msg = f"{source}: {_describe(exc)}"
        raise ConfigError(msg) from None
    except templates.TemplateError as exc:  # pragma: no cover - wrapped by pydantic
        msg = f"{source}: {exc}"
        raise ConfigError(msg) from None


def load_config(path: Path) -> HttpTargetConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = f"target config not found: {path}"
        raise ConfigError(msg) from None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read {path}: {type(exc).__name__}"
        raise ConfigError(msg) from None
    config = parse_config(text, str(path))
    if config.ca_file is not None and not (path.parent / config.ca_file).is_file():
        msg = f"{path}: ca_file {config.ca_file!r} not found next to the config"
        raise ConfigError(msg)
    return config
