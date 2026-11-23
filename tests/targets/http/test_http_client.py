import json

import httpx
import pytest

from pecker_drum.targets.http.client import HttpSession, RateLimiter, Redactor, TargetError
from pecker_drum.targets.http.config import HttpTargetConfig


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_rate_limiter_keeps_to_n_per_minute() -> None:
    clock = Clock()
    limiter = RateLimiter(3, clock, clock.sleep)
    for _ in range(3):
        await limiter.wait()
    assert clock.slept == []
    await limiter.wait()  # the fourth start waits until the first leaves the window
    assert clock.slept == [60.0]
    clock.now += 10
    await limiter.wait()
    assert clock.slept == [60.0]  # the window from 0 has moved on: slots free again
    assert len(clock.slept) == 1


def test_redactor() -> None:
    redactor = Redactor()
    redactor.add("abc")  # too short to redact without mangling ordinary text
    redactor.add("secret-token-1")
    redactor.add("secret-token-12")
    assert redactor("a secret-token-12 b secret-token-1 abc") == "a [redacted] b [redacted] abc"
    assert redactor.json({"secret-token-1": ["secret-token-1", 3]}) == {
        "[redacted]": ["[redacted]", 3]
    }


def session(
    config: HttpTargetConfig, handler: httpx.MockTransport, redactor: Redactor | None = None
) -> HttpSession:
    return HttpSession(config, redactor or Redactor(), transport=handler)


async def test_requests_stay_on_the_configured_origins(example_config: HttpTargetConfig) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async with session(example_config, httpx.MockTransport(handle)) as http:
        assert await http.call("probe", "GET", http.url("/api/x")) == {"ok": True}
        for url in (
            "http://127.0.0.1:9999/api",
            "https://127.0.0.1:8080/api",
            "http://evil.example/",
        ):
            with pytest.raises(TargetError, match="outside the configured origins"):
                await http.call("probe", "GET", url)
    assert seen == ["http://127.0.0.1:8080/api/x"]


async def test_redirects_are_not_followed(example_config: HttpTargetConfig) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://evil.example/"})

    async with session(example_config, httpx.MockTransport(handle)) as http:
        with pytest.raises(TargetError, match="HTTP 302") as raised:
            await http.call("probe", "GET", http.url("/x"))
    assert raised.value.status == 302


async def test_errors_name_the_status_and_redact_the_body(example_config: HttpTargetConfig) -> None:
    redactor = Redactor()
    redactor.add("tok-0123456789")

    def handle(request: httpx.Request) -> httpx.Response:
        body = "cost cap reached for tok-0123456789 " + "x" * 500
        return httpx.Response(429, text=body)

    async with session(example_config, httpx.MockTransport(handle), redactor) as http:
        with pytest.raises(TargetError) as raised:
            await http.call("send message", "POST", http.url("/m"))
        message = str(raised.value)
        assert raised.value.status == 429
        assert message.startswith("send message: HTTP 429: cost cap reached for [redacted]")
        assert "tok-0123456789" not in message
        assert len(message) < 260
        with pytest.raises(TargetError) as login:
            await http.call("login as carol", "POST", example_config.token_url(), login=True)
        assert str(login.value) == "login as carol: HTTP 429"  # never a login body


async def test_response_size_cap(example_config: HttpTargetConfig) -> None:
    capped = example_config.model_copy(
        update={"limits": example_config.limits.model_copy(update={"max_response_bytes": 1024})}
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"x": "y" * 2000}))

    async with session(capped, httpx.MockTransport(handle)) as http:
        with pytest.raises(TargetError, match="larger than 1024 bytes"):
            await http.call("list documents", "GET", http.url("/d"))


async def test_stream_caps(example_config: HttpTargetConfig) -> None:
    capped = example_config.model_copy(
        update={"limits": example_config.limits.model_copy(update={"max_stream_events": 2})}
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "text/event-stream"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, content=b"data: 1\n\n" * 5)

    async with (
        session(capped, httpx.MockTransport(handle)) as http,
        http.stream("send message", "POST", http.url("/m"), token="tok", json_body={}) as events,
    ):
        with pytest.raises(TargetError, match="send message: stream has more than 2 events"):
            _ = [event async for event in events]


async def test_transport_errors_become_target_errors(example_config: HttpTargetConfig) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with session(example_config, httpx.MockTransport(handle)) as http:
        with pytest.raises(
            TargetError, match="create conversation: ConnectError: connection refused"
        ):
            await http.call("create conversation", "POST", http.url("/c"))
