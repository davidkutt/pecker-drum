"""Block outbound network connections for offline runs (tests and fake-model self-tests).

``block_network(allow_loopback=True)`` lets connections to the literal address ``127.0.0.1``
through and nothing else: the stand-in server of ``--target stub`` (M2) and the tests that start it
listen there. No name is resolved, not even ``localhost``.
"""

from __future__ import annotations

import socket
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

_LOCAL_FAMILIES = {socket.AF_UNIX}
LOOPBACK_HOST = "127.0.0.1"
_state = {"loopback": False}


class NetworkBlockedError(RuntimeError):
    """Raised when code tries to open a network connection while the guard is active."""


_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


def _is_loopback(address: object) -> bool:
    if not _state["loopback"] or not isinstance(address, tuple):
        return False
    parts = cast("tuple[object, ...]", address)
    return len(parts) >= 2 and parts[0] == LOOPBACK_HOST


def _blocked_connect(self: socket.socket, address: Any) -> None:
    if self.family in _LOCAL_FAMILIES or (self.family == socket.AF_INET and _is_loopback(address)):
        _original_connect(self, address)
        return
    msg = f"network access is blocked in offline mode (tried to connect to {address!r})"
    raise NetworkBlockedError(msg)


def _blocked_connect_ex(self: socket.socket, address: Any) -> int:
    if self.family in _LOCAL_FAMILIES or (self.family == socket.AF_INET and _is_loopback(address)):
        return _original_connect_ex(self, address)
    _blocked_connect(self, address)
    return 0


def _blocked_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if _state["loopback"] and host in (LOOPBACK_HOST, LOOPBACK_HOST.encode()):
        return _original_getaddrinfo(host, *args, **kwargs)
    msg = f"network access is blocked in offline mode (tried to resolve {host!r})"
    raise NetworkBlockedError(msg)


def block_network(*, allow_loopback: bool = False) -> None:
    """Install the guard for the rest of the process.

    With ``allow_loopback`` only ``127.0.0.1`` stays reachable (IPv4, given as that literal).
    """
    _state["loopback"] = allow_loopback
    socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _blocked_getaddrinfo


def allow_network() -> None:
    """Remove the guard."""
    _state["loopback"] = False
    socket.socket.connect = _original_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _original_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _original_getaddrinfo


@contextmanager
def offline(*, allow_loopback: bool = False) -> Generator[None]:
    """Block network access inside the `with` block."""
    block_network(allow_loopback=allow_loopback)
    try:
        yield
    finally:
        allow_network()
