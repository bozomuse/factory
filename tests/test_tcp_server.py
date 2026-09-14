"""Behavior tests for the TLS TCP transport."""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from factory.tcp_server import (
    CertificateLoadError,
    InvalidServerConfigError,
    SslTcpServer,
    create_server_context,
)


class RecordingProtocol:
    def __init__(self, response: bytes | None = b"response") -> None:
        self.messages: list[bytes] = []
        self.response = response
        self.closed = False

    def open(self, sender: Callable[[bytes], None]) -> RecordingProtocol:
        return self

    def handle(self, message: bytes) -> bytes | None:
        self.messages.append(message)
        return self.response

    def close(self) -> None:
        self.closed = True


def _context() -> ssl.SSLContext:
    return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)


def test_create_exposes_configured_address() -> None:
    server = SslTcpServer.create(
        context=_context(),
        protocol=RecordingProtocol(),
        host="localhost",
        port=1234,
    )

    assert server.address == ("localhost", 1234)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"host": ""}, "host"),
        ({"port": -1}, "port"),
        ({"port": 65_536}, "port"),
        ({"backlog": 0}, "backlog"),
        ({"max_message_bytes": 0}, "max_message_bytes"),
    ],
)
def test_create_rejects_invalid_config(
    overrides: dict[str, object],
    message: str,
) -> None:
    arguments: dict[str, object] = {
        "context": _context(),
        "protocol": RecordingProtocol(),
        **overrides,
    }

    with pytest.raises(InvalidServerConfigError, match=message):
        SslTcpServer.create(**arguments)  # type: ignore[arg-type]


def test_create_server_context_wraps_load_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenContext:
        def load_cert_chain(self, certfile: Path, keyfile: Path) -> None:
            raise OSError(certfile, keyfile)

    def create_context(protocol: object) -> BrokenContext:
        return BrokenContext()

    monkeypatch.setattr(ssl, "SSLContext", create_context)
    with pytest.raises(CertificateLoadError, match=r"certificate\.pem"):
        create_server_context(Path("certificate.pem"), Path("key.pem"))


def test_create_server_context_loads_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingContext:
        def __init__(self) -> None:
            self.loaded: tuple[Path, Path] | None = None

        def load_cert_chain(self, certfile: Path, keyfile: Path) -> None:
            self.loaded = certfile, keyfile

    context = RecordingContext()

    def create_context(protocol: object) -> RecordingContext:
        return context

    monkeypatch.setattr(ssl, "SSLContext", create_context)

    result = create_server_context(Path("certificate.pem"), Path("key.pem"))

    assert result is context
    assert context.loaded == (Path("certificate.pem"), Path("key.pem"))


def test_serve_forever_frames_multiple_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = RecordingProtocol()
    server_holder: list[SslTcpServer] = []

    class SecureConnection:
        def __init__(self) -> None:
            self.chunks = [b"first\nsec", b"ond\n"]
            self.sent: list[bytes] = []

        def __enter__(self) -> SecureConnection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def recv(self, size: int) -> bytes:
            if self.chunks:
                return self.chunks.pop(0)
            server_holder[0].close()
            return b""

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def close(self) -> None:
            return None

    secure_connection = SecureConnection()

    class Context:
        def wrap_socket(
            self,
            connection: socket.socket,
            *,
            server_side: bool,
        ) -> SecureConnection:
            assert server_side
            return secure_connection

    class RawConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    raw_connection = RawConnection()

    class Listener:
        def __init__(self) -> None:
            self.bound_to: tuple[str, int] | None = None
            self.backlog: int | None = None
            self.closed = False
            self.accepted = False
            self.closed_event = threading.Event()

        def __enter__(self) -> Listener:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def setsockopt(self, level: int, option: int, value: int) -> None:
            return None

        def bind(self, address: tuple[str, int]) -> None:
            self.bound_to = address

        def listen(self, backlog: int) -> None:
            self.backlog = backlog

        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            if not self.accepted:
                self.accepted = True
                return cast(socket.socket, raw_connection), ("127.0.0.1", 5000)
            self.closed_event.wait(2)
            raise OSError("listener closed")

        def close(self) -> None:
            self.closed = True
            self.closed_event.set()

    listener = Listener()

    def create_socket(*args: object) -> Listener:
        return listener

    monkeypatch.setattr(socket, "socket", create_socket)
    server = SslTcpServer.create(
        context=cast(ssl.SSLContext, Context()),
        protocol=protocol,
        host="localhost",
        port=1234,
    )
    server_holder.append(server)

    server.serve_forever()

    assert listener.bound_to == ("localhost", 1234)
    assert listener.backlog == 128
    assert listener.closed
    assert raw_connection.closed
    assert protocol.messages == [b"first", b"second"]
    assert protocol.closed
    assert secure_connection.sent == [b"response\n", b"response\n"]


def test_close_before_serving_is_safe() -> None:
    server = SslTcpServer.create(context=_context(), protocol=RecordingProtocol())

    server.close()
