"""The HTTP session of an HTTP target: limits, origin checks and redaction (M2, ADR 0009).

- Every request waits for the requests-per-minute limiter, logins and polling included.
- Requests go only to the origin of ``base_url`` or of the token URL. Redirects are never
  followed, proxy variables and ``.netrc`` are ignored (``trust_env=False``), TLS is always
  verified.
- A response body is read up to ``max_response_bytes``; a stream up to ``max_stream_bytes`` and
  ``max_stream_events``. Going over a cap is an error.
- An error from an HTTP response names the step and the status code. Body text is redacted and
  cut to an excerpt, and never shown for login requests. There are no automatic retries.
- ``Redactor`` replaces every password and token the session knows with ``[redacted]``.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from pecker_drum.targets.http.config import HttpTargetConfig
from pecker_drum.targets.http.sse import SseEvent, SseLimitError, SseParser

REDACTED = "[redacted]"
ERROR_BODY_CHARS = 200
_MIN_SECRET = 4

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TargetError(Exception):
    """A request to the target failed. The message is redacted and safe to record."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """The HTTP status code, when the target answered."""


class Redactor:
    """Knows every secret of a run (passwords, client secret, tokens) and removes them."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= _MIN_SECRET:
            self._secrets.add(secret)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def json(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.json(element) for element in value]
        if isinstance(value, dict):
            return {self(key): self.json(element) for key, element in value.items()}
        return value


class RateLimiter:
    """At most ``per_minute`` request starts in any 60-second window."""

    def __init__(self, per_minute: int, clock: Clock, sleep: Sleep) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and self._starts[0] <= now - 60.0:
                    self._starts.popleft()
                if len(self._starts) < self._per_minute:
                    self._starts.append(now)
                    return
                await self._sleep(self._starts[0] + 60.0 - now)


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or "").lower(), port


class HttpSession:
    def __init__(
        self,
        config: HttpTargetConfig,
        redactor: Redactor,
        *,
        ca_file: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._config = config
        self._redact = redactor
        self._limits = config.limits
        self._limiter = RateLimiter(config.limits.requests_per_minute, clock, sleep)
        self._origins = {_origin(config.base_url), _origin(config.token_url())}
        self._base = config.base_url.rstrip("/")
        timeouts = config.timeouts
        self._timeout = httpx.Timeout(
            connect=timeouts.connect, read=timeouts.read, write=timeouts.read, pool=timeouts.connect
        )
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else True
        )
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpSession:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            verify=self._verify,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def url(self, path: str) -> str:
        """``base_url`` plus a rendered path (its variables are already percent-encoded)."""
        return self._base + path

    def _check(self, url: str, step: str) -> httpx.AsyncClient:
        if self._client is None:
            msg = f"{step}: the HTTP session is not open"
            raise TargetError(msg)
        if _origin(url) not in self._origins:
            msg = f"{step}: refused a request outside the configured origins"
            raise TargetError(msg)
        return self._client

    def _failure(self, step: str, exc: httpx.HTTPError) -> TargetError:
        detail = self._redact(str(exc))[:ERROR_BODY_CHARS]
        return TargetError(f"{step}: {type(exc).__name__}" + (f": {detail}" if detail else ""))

    def _status_error(self, step: str, status: int, body: bytes, *, show_body: bool) -> TargetError:
        message = f"{step}: HTTP {status}"
        if show_body and body:
            text = " ".join(body.decode("utf-8", errors="replace").split())
            excerpt = self._redact(text)[:ERROR_BODY_CHARS]
            if excerpt:
                message += f": {excerpt}"
        return TargetError(message, status)

    async def _read(self, response: httpx.Response, step: str) -> bytes:
        cap = self._limits.max_response_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > cap:
                msg = f"{step}: response larger than {cap} bytes"
                raise TargetError(msg)
            chunks.append(chunk)
        return b"".join(chunks)

    async def call(
        self,
        step: str,
        method: str,
        url: str,
        *,
        token: str | None = None,
        json_body: JsonValue | None = None,
        form: Mapping[str, str | list[str]] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        login: bool = False,
    ) -> JsonValue:
        """One request; returns the parsed JSON body (``None`` for an empty body)."""
        client = self._check(url, step)
        await self._limiter.wait()
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if form is not None:
            kwargs["data"] = dict(form)
        if files is not None:
            kwargs["files"] = dict(files)
        try:
            request = client.build_request(method, url, **kwargs)
            response = await client.send(request, stream=True)
            try:
                body = await self._read(response, step)
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            raise self._failure(step, exc) from None
        if not 200 <= response.status_code < 300:
            raise self._status_error(step, response.status_code, body, show_body=not login)
        if not body.strip():
            return None
        try:
            return cast("JsonValue", json.loads(body))
        except ValueError:
            msg = f"{step}: response is not JSON"
            raise TargetError(msg) from None

    @asynccontextmanager
    async def stream(
        self, step: str, method: str, url: str, *, token: str, json_body: JsonValue | None
    ) -> AsyncGenerator[AsyncGenerator[SseEvent]]:
        """Open an event stream; the response is closed when the block ends."""
        client = self._check(url, step)
        await self._limiter.wait()
        headers = {"Accept": "text/event-stream", "Authorization": f"Bearer {token}"}
        try:
            request = client.build_request(method, url, headers=headers, json=json_body)
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise self._failure(step, exc) from None
        try:
            if not 200 <= response.status_code < 300:
                body = await self._read(response, step)
                raise self._status_error(step, response.status_code, body, show_body=True)
            yield self._events(response, step)
        except httpx.HTTPError as exc:
            raise self._failure(step, exc) from None
        finally:
            await response.aclose()

    async def _events(self, response: httpx.Response, step: str) -> AsyncGenerator[SseEvent]:
        parser = SseParser(
            max_bytes=self._limits.max_stream_bytes, max_events=self._limits.max_stream_events
        )
        try:
            async for chunk in response.aiter_bytes():
                for event in parser.feed(chunk):
                    yield event
            for event in parser.close():
                yield event
        except SseLimitError as exc:
            raise TargetError(f"{step}: {exc}") from None
        except httpx.HTTPError as exc:
            raise self._failure(step, exc) from None
