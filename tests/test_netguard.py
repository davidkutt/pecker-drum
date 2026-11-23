import socket

import pytest

from pecker_drum.netguard import NetworkBlockedError, block_network


def test_tcp_connect_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect(("192.0.2.1", 443))


def test_dns_lookup_is_blocked() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("example.invalid", 443)


def test_local_socketpair_still_works() -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"ok")
        assert right.recv(2) == b"ok"


def test_loopback_stays_blocked_unless_allowed() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect(("127.0.0.1", 9))
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("127.0.0.1", 80)


def test_allow_loopback_lets_only_127_0_0_1_through() -> None:
    block_network(allow_loopback=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))
        assert socket.getaddrinfo("127.0.0.1", port)
    for host in ("127.0.0.2", "192.0.2.1"):
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
            pytest.raises(NetworkBlockedError),
        ):
            sock.connect((host, 443))
    for name in ("localhost", "example.invalid"):
        with pytest.raises(NetworkBlockedError):
            socket.getaddrinfo(name, 443)
    block_network()
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkBlockedError),
    ):
        sock.connect(("127.0.0.1", 9))
