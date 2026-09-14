"""Behavior tests for the default Factory client."""

from __future__ import annotations

import socket
import ssl
from pathlib import Path
from typing import cast

import pytest

from factory.client import (
    ClientConfig,
    FactoryClient,
    InvalidClientConfigError,
    RemoteOperationError,
)


class FakeConnection:
    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeSecureConnection:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.sent: list[bytes] = []

    def __enter__(self) -> FakeSecureConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


def _install_connection(
    monkeypatch: pytest.MonkeyPatch,
    secure: FakeSecureConnection,
) -> None:
    class Context:
        def wrap_socket(
            self,
            connection: socket.socket,
            *,
            server_hostname: str,
        ) -> FakeSecureConnection:
            assert server_hostname == "factory.test"
            return secure

    def create_context(**kwargs: object) -> Context:
        return Context()

    def create_connection(
        address: tuple[str, int],
        *,
        timeout: float,
    ) -> socket.socket:
        assert timeout == 10.0
        return cast(socket.socket, FakeConnection())

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(
        socket,
        "create_connection",
        create_connection,
    )


def _config() -> ClientConfig:
    return ClientConfig.create(
        host="localhost",
        port=8443,
        certificate=Path("certificate.pem"),
        server_name="factory.test",
    )


def test_client_config_validates_address() -> None:
    with pytest.raises(InvalidClientConfigError):
        ClientConfig.create(host="", certificate=Path("certificate.pem"))
    with pytest.raises(InvalidClientConfigError):
        ClientConfig.create(
            host="localhost",
            port=0,
            certificate=Path("certificate.pem"),
        )
    with pytest.raises(InvalidClientConfigError):
        ClientConfig.create(
            host="localhost",
            certificate=Path("certificate.pem"),
            timeout=0,
        )


def test_call_returns_matching_result(monkeypatch: pytest.MonkeyPatch) -> None:
    secure = FakeSecureConnection(
        [b'{"jsonrpc":"2.0","result":{"channels":[]},"id":1}\n']
    )
    _install_connection(monkeypatch, secure)

    result = FactoryClient(_config()).call("factory.state")

    assert result == {"channels": []}
    assert secure.sent == [b'{"jsonrpc":"2.0","method":"factory.state","id":1}\n']


def test_call_raises_remote_error(monkeypatch: pytest.MonkeyPatch) -> None:
    secure = FakeSecureConnection(
        [b'{"jsonrpc":"2.0","error":{"code":-32000,"message":"failed"},"id":1}\n']
    )
    _install_connection(monkeypatch, secure)

    with pytest.raises(RemoteOperationError, match="failed"):
        FactoryClient(_config()).call("factory.state")


def test_notifications_preserve_multiple_messages_from_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = (
        b'{"jsonrpc":"2.0","result":{"subscribed":true,"history":['
        b'{"sequence":1,"event":"old","timestamp":"t","data":{}}]},"id":1}\n'
        b'{"jsonrpc":"2.0","method":"factory.notification","params":'
        b'{"sequence":2,"event":"new","timestamp":"t","data":{}}}\n'
    )
    secure = FakeSecureConnection([response])
    _install_connection(monkeypatch, secure)
    events = FactoryClient(_config()).notifications()

    assert next(events)["event"] == "old"
    assert next(events)["event"] == "new"
