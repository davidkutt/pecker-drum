import pytest

from pecker_drum.targets.http.sse import SseEvent, SseLimitError, SseParser

STREAM = (
    "\ufeff: comment line\n"
    'event: token\ndata: {"delta": "Grüße "}\n\n'
    'event: token\r\ndata: {"delta": "👋 done"}\r\n\r\n'
    "id: 7\nretry: 100\nevent: sources\ndata: line one\ndata: line two\n\n"
    "event: empty\n\n"
    "data\n\n"
    "data:no space\ndata:  two spaces\n\n"
    "unknown: field\nevent: done\rdata: {}\r\r"
).encode()
EXPECTED = [
    SseEvent("token", '{"delta": "Grüße "}'),
    SseEvent("token", '{"delta": "👋 done"}'),
    SseEvent("sources", "line one\nline two"),
    SseEvent("message", ""),
    SseEvent("message", "no space\n two spaces"),
    SseEvent("done", "{}"),
]


def parse(chunks: list[bytes], **caps: int) -> list[SseEvent]:
    parser = SseParser(**caps)
    events: list[SseEvent] = []
    for chunk in chunks:
        events += parser.feed(chunk)
    return events + parser.close()


def test_whole_stream() -> None:
    assert parse([STREAM]) == EXPECTED


def test_split_at_every_byte_gives_the_same_events() -> None:
    """Splits land inside UTF-8 characters, inside CRLF pairs and inside field names."""
    for cut in range(1, len(STREAM)):
        assert parse([STREAM[:cut], STREAM[cut:]]) == EXPECTED, cut


def test_one_byte_at_a_time() -> None:
    assert parse([STREAM[i : i + 1] for i in range(len(STREAM))]) == EXPECTED


def test_an_unfinished_event_at_the_end_is_discarded() -> None:
    assert parse([b'event: token\ndata: {}\n\nevent: token\ndata: {"partial"']) == [
        SseEvent("token", "{}")
    ]


def test_invalid_utf8_becomes_replacement_characters() -> None:
    assert parse([b"data: \xff\xfe ok\n\n"]) == [SseEvent("message", "�� ok")]


def test_byte_and_event_caps() -> None:
    with pytest.raises(SseLimitError, match="larger than 2048 bytes"):
        parse([b"data: x\n\n" * 300], max_bytes=2048)
    with pytest.raises(SseLimitError, match="more than 3 events"):
        parse([b"data: x\n\n" * 4], max_events=3)
    assert len(parse([b"data: x\n\n" * 3], max_events=3)) == 3
