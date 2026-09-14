"""Tests for the JSON-RPC test client."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
from pathlib import Path
from typing import cast

import pytest

from scripts import tcp_client


def _args(*, notification: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        hostname="localhost",
        certificate=Path("certificate.pem"),
        port=8443,
        method="echo",
        params='{"value":1}',
        id="request-1",
        notification=notification,
    )


def test_builds_compact_request() -> None:
    request = tcp_client._request_bytes(_args())  # pyright: ignore[reportPrivateUsage]

    assert request == (
        b'{"jsonrpc":"2.0","method":"echo","id":"request-1","params":{"value":1}}'
    )


def test_builds_notification_without_id() -> None:
    request = tcp_client._request_bytes(  # pyright: ignore[reportPrivateUsage]
        _args(notification=True)
    )

    assert "id" not in json.loads(request)


def test_rejects_scalar_params() -> None:
    args = _args()
    args.params = "1"

    with pytest.raises(argparse.ArgumentTypeError):
        tcp_client._request_bytes(args)  # pyright: ignore[reportPrivateUsage]


def test_main_sends_request_and_prints_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class SecureConnection:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.chunks = [b'{"result":', b'"ok"}\n']

        def __enter__(self) -> SecureConnection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, size: int) -> bytes:
            return self.chunks.pop(0)

    secure_connection = SecureConnection()

    class Context:
        def wrap_socket(
            self,
            connection: socket.socket,
            *,
            server_hostname: str,
        ) -> SecureConnection:
            assert server_hostname == "localhost"
            return secure_connection

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def create_context(**kwargs: object) -> Context:
        return Context()

    def create_connection(address: tuple[str, int]) -> socket.socket:
        return cast(socket.socket, Connection())

    monkeypatch.setattr(tcp_client, "_parse_args", _args)
    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(socket, "create_connection", create_connection)

    tcp_client.main()

    request = tcp_client._request_bytes(_args())  # pyright: ignore[reportPrivateUsage]
    assert secure_connection.sent == [request + b"\n"]
    assert capsys.readouterr().out == '{"result":"ok"}\n'
