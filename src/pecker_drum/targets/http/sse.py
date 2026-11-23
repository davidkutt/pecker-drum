"""A server-sent-events parser that follows the WHATWG event-stream rules (M2).

Bytes may arrive split anywhere, even inside a UTF-8 character or between ``\\r`` and ``\\n``.
Lines end with CRLF, LF or CR; a leading byte-order mark is dropped; lines starting with ``:``
are comments; several ``data:`` lines join with ``\\n``; ``id`` and ``retry`` are ignored; an
event without data is not dispatched, and an unfinished event at the end of the stream is
discarded. Invalid UTF-8 becomes U+FFFD. Everything parsed here is hostile target output.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

_LINE_END = re.compile(r"\r\n|\r|\n")
_BOM = "﻿"


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str
    """The ``event:`` field, or ``message`` when the stream gave none."""
    data: str


class SseLimitError(Exception):
    """The stream went over a byte or event cap."""


class SseParser:
    def __init__(self, *, max_bytes: int | None = None, max_events: int | None = None) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._skip_lf = False
        self._started = False
        self._event = ""
        self._data: list[str] = []
        self._bytes = 0
        self._events = 0
        self._max_bytes = max_bytes
        self._max_events = max_events

    def feed(self, chunk: bytes) -> list[SseEvent]:
        self._bytes += len(chunk)
        if self._max_bytes is not None and self._bytes > self._max_bytes:
            msg = f"stream larger than {self._max_bytes} bytes"
            raise SseLimitError(msg)
        return self._text(self._decoder.decode(chunk))

    def close(self) -> list[SseEvent]:
        """The end of the stream: complete lines still dispatch, an unfinished event does not."""
        return self._text(self._decoder.decode(b"", final=True))

    def _text(self, text: str) -> list[SseEvent]:
        if not self._started and text:
            self._started = True
            text = text.removeprefix(_BOM)
        if self._skip_lf and text:
            self._skip_lf = False
            text = text.removeprefix("\n")
        buffer = self._buffer + text
        events: list[SseEvent] = []
        start = 0
        for match in _LINE_END.finditer(buffer):
            event = self._line(buffer[start : match.start()])
            if event is not None:
                events.append(event)
            start = match.end()
            if match.group(0) == "\r" and start == len(buffer):
                self._skip_lf = True  # a CRLF may be split between two chunks
        self._buffer = buffer[start:]
        return events

    def _line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data:
            self._event = ""
            return None
        event = SseEvent(self._event or "message", "\n".join(self._data))
        self._event = ""
        self._data = []
        self._events += 1
        if self._max_events is not None and self._events > self._max_events:
            msg = f"stream has more than {self._max_events} events"
            raise SseLimitError(msg)
        return event
