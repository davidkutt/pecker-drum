"""The stand-in server's own behaviour (the adapter tests cover the happy paths)."""

import json
import time
from collections.abc import Generator

import httpx
import pytest

from pecker_drum.items.schema import Item
from pecker_drum.netguard import block_network
from pecker_drum.targets.stub import StubMode, StubOptions, StubServer
from pecker_drum.targets.stub.server import NO_SCRIPT_ANSWER, parse_multipart

ITEM = Item.model_validate(
    {
        "id": "t-001",
        "title": "t",
        "vector": "direct",
        "lang": "en",
        "severity": "high",
        "user": {"sub": "carol"},
        "turns": ["hello"],
        "succeeds_if": [{"kind": "answer_matches", "pattern": "x"}],
        "origin": "hand",
        "self_test": {"answer": "obedient answer"},
    }
)


WRITE = Item.model_validate(
    ITEM.model_dump()
    | {
        "id": "t-002",
        "turns": ["file it"],
        "self_test": {"tool_calls": [{"name": "repo_create_issue", "args": {"title": "x"}}]},
    }
)


@pytest.fixture
def server() -> Generator[StubServer]:
    block_network(allow_loopback=True)
    users = {"carol": "pw-c", "dana": "pw-d"}
    with StubServer([ITEM, WRITE], StubMode.OBEDIENT, users, StubOptions(close_delay=0.3)) as up:
        yield up


def login(client: httpx.Client, user: str, password: str) -> httpx.Response:
    form = {"grant_type": "password", "client_id": "c", "username": user, "password": password}
    return client.post("/oauth/token", data=form)


def test_binds_loopback_on_a_random_port(server: StubServer) -> None:
    assert server.url.startswith("http://127.0.0.1:")
    assert not server.url.endswith(":8080")


def test_token_endpoint(server: StubServer) -> None:
    with httpx.Client(base_url=server.url, trust_env=False) as client:
        assert login(client, "carol", "wrong").status_code == 401
        reply = login(client, "carol", "pw-c").json()
        assert reply["token_type"] == "Bearer"
        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "c",
                "refresh_token": reply["refresh_token"],
            },
        )
        assert refreshed.status_code == 200
        reused = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "c",
                "refresh_token": reply["refresh_token"],
            },
        )
        assert reused.status_code == 400  # refresh tokens rotate
        assert client.get("/api/documents").status_code == 401


def test_documents_are_listed_for_everyone_and_deleted_by_their_owner(server: StubServer) -> None:
    with httpx.Client(base_url=server.url, trust_env=False) as client:
        carol = {"Authorization": "Bearer " + login(client, "carol", "pw-c").json()["access_token"]}
        dana = {"Authorization": "Bearer " + login(client, "dana", "pw-d").json()["access_token"]}
        missing = client.post("/api/documents", headers=carol, files={"file": ("a.md", b"x")})
        assert missing.status_code == 400  # the static collection field is required
        created = client.post(
            "/api/documents",
            headers=carol,
            data={"collection": "c"},
            files={"file": ("a.md", b"text")},
        ).json()
        (listed,) = client.get("/api/documents", headers=dana).json()["documents"]
        assert listed["filename"] == "a.md"
        assert client.delete(f"/api/documents/{created['id']}", headers=dana).status_code == 403
        assert client.delete(f"/api/documents/{created['id']}", headers=carol).status_code == 204
        assert client.delete(f"/api/documents/{created['id']}", headers=carol).status_code == 404


def test_uploads_can_be_limited_to_some_users() -> None:
    block_network(allow_loopback=True)
    users = {"carol": "pw-c", "dana": "pw-d"}
    options = StubOptions(uploaders=frozenset({"dana"}))
    with (
        StubServer([ITEM], StubMode.OBEDIENT, users, options) as up,
        httpx.Client(base_url=up.url, trust_env=False) as client,
    ):
        for user, status in (("carol", 403), ("dana", 201)):
            token = login(client, user, users[user]).json()["access_token"]
            reply = client.post(
                "/api/documents",
                headers={"Authorization": f"Bearer {token}"},
                data={"collection": "c"},
                files={"file": ("a.md", b"text")},
            )
            assert reply.status_code == status, user


def test_messages_need_the_static_model_field_and_a_known_script(server: StubServer) -> None:
    with httpx.Client(base_url=server.url, trust_env=False) as client:
        carol = {"Authorization": "Bearer " + login(client, "carol", "pw-c").json()["access_token"]}
        assert client.post("/api/conversations", headers=carol, json={}).status_code == 400
        conversation = client.post("/api/conversations", headers=carol, json={"model": "m"}).json()
        path = f"/api/conversations/{conversation['id']}/messages"
        assert client.post(path, headers=carol, json={"content": "hello"}).status_code == 400
        stream = client.post(path, headers=carol, json={"model": "m", "content": "something else"})
        assert stream.headers["Content-Type"].startswith("text/event-stream")
        assert NO_SCRIPT_ANSWER.split(" ")[0] in stream.text
        assert "event: done" in stream.text
        bad = client.post("/api/approvals/nope/decision", headers=carol, json={"decision": "maybe"})
        assert bad.status_code == 400
        unknown = client.post(
            "/api/approvals/nope/decision", headers=carol, json={"decision": "reject"}
        )
        assert unknown.status_code == 404


def _conversation(client: httpx.Client, headers: dict[str, str]) -> str:
    conversation = client.post("/api/conversations", headers=headers, json={"model": "m"}).json()
    return f"/api/conversations/{conversation['id']}/messages"


def test_an_approval_is_stored_only_when_its_stream_closes(server: StubServer) -> None:
    with httpx.Client(base_url=server.url, trust_env=False) as client:
        carol = {"Authorization": "Bearer " + login(client, "carol", "pw-c").json()["access_token"]}
        path = _conversation(client, carol)
        events: list[str] = []
        urls: list[str] = []
        early: list[int] = []
        deny = {"decision": "reject"}
        with client.stream(
            "POST", path, headers=carol, json={"model": "m", "content": "file it"}
        ) as r:
            for line in r.iter_lines():
                if line.startswith("event: "):
                    events.append(line.removeprefix("event: "))
                if line.startswith("data: ") and events[-1] == "approval_required":
                    approval = json.loads(line.removeprefix("data: "))["approval_id"]
                    urls.append(f"/api/approvals/{approval}/decision")
                    early.append(client.post(urls[-1], headers=carol, json=deny).status_code)
        assert events[-2:] == ["approval_required", "done"]  # done follows the approval request
        assert early == [404]  # sent before the stream closed: the approval was not stored yet
        late = client.post(urls[0], headers=carol, json=deny)
        assert late.status_code == 200
        assert "event: tool_result" in late.text
    assert server.state.disconnects == 0


def test_a_client_that_disconnects_early_cancels_the_turn(server: StubServer) -> None:
    with httpx.Client(base_url=server.url, trust_env=False) as client:
        carol = {"Authorization": "Bearer " + login(client, "carol", "pw-c").json()["access_token"]}
        path = _conversation(client, carol)
        with client.stream(
            "POST", path, headers=carol, json={"model": "m", "content": "file it"}
        ) as r:
            for line in r.iter_lines():
                if line == "event: done":
                    break  # stop reading at done and close the connection
        deadline = time.monotonic() + 5
        while not server.state.disconnects and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.state.disconnects == 1
        assert server.state.pending == {}  # the turn was cancelled: no approval to deny


def test_parse_multipart_keeps_repeated_fields() -> None:
    request = httpx.Request(
        "POST",
        "http://127.0.0.1/",
        data={"tags": ["a", "b"], "collection": "c"},
        files={"file": ("x.md", "Grüße".encode(), "text/markdown")},
    )
    body = request.read()
    fields, files = parse_multipart(request.headers["Content-Type"], body)
    assert fields == {"tags": ["a", "b"], "collection": ["c"]}
    assert files == [("file", "x.md", "Grüße".encode())]


def test_options_default_to_well_behaved() -> None:
    options = StubOptions()
    assert not options.ignore_deletes
    assert not options.never_ready
    assert not options.leaky
