"""Default TLS JSON-RPC client used by the Factory control CLI."""

from __future__ import annotations

import json
import math
import socket
import ssl
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from factory.jsonrpc import JsonObject, JsonParams, JsonValue

_MAX_MESSAGE_BYTES = 1_048_576


class FactoryClientError(Exception):
    """Base class for client connection and protocol errors."""


class InvalidClientConfigError(FactoryClientError):
    """Raised when client connection settings are invalid."""


class FactoryConnectionError(FactoryClientError):
    """Raised when the server closes or sends an invalid message."""


class RemoteOperationError(FactoryClientError):
    """Raised when a JSON-RPC call returns an error."""


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Validated connection settings for a Factory server."""

    host: str
    port: int
    certificate: Path
    server_name: str

    @classmethod
    def create(
        cls,
        *,
        host: str,
        certificate: Path,
        port: int = 8443,
        server_name: str | None = None,
    ) -> ClientConfig:
        """Validate and create client connection settings."""
        if not host:
            raise InvalidClientConfigError("host must not be empty")
        if not 1 <= port <= 65_535:
            raise InvalidClientConfigError("port must be between 1 and 65535")
        return cls(host, port, certificate, server_name or host)


class FactoryClient:
    """Make JSON-RPC calls to a remote Factory server."""

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._next_id = 1

    def call(self, method: str, params: JsonParams | None = None) -> JsonValue:
        """Call one method over a short-lived verified TLS connection."""
        request_id = self._take_id()
        request = _request(method, params, request_id)
        context = ssl.create_default_context(cafile=str(self._config.certificate))
        try:
            with (
                socket.create_connection(
                    (self._config.host, self._config.port)
                ) as connection,
                context.wrap_socket(
                    connection,
                    server_hostname=self._config.server_name,
                ) as secure_connection,
            ):
                secure_connection.sendall(_encode(request) + b"\n")
                buffer = bytearray()
                while True:
                    response = _read_json(secure_connection, buffer)
                    if response.get("id") == request_id:
                        return _response_result(response)
        except OSError as error:
            raise FactoryConnectionError(str(error)) from error

    def notifications(self, after: int = 0) -> Iterator[JsonObject]:
        """Yield durable history followed by live server notifications."""
        if after < 0:
            raise InvalidClientConfigError("after must not be negative")
        request_id = self._take_id()
        request = _request("notification.subscribe", {"after": after}, request_id)
        context = ssl.create_default_context(cafile=str(self._config.certificate))
        try:
            with (
                socket.create_connection(
                    (self._config.host, self._config.port)
                ) as connection,
                context.wrap_socket(
                    connection,
                    server_hostname=self._config.server_name,
                ) as secure_connection,
            ):
                secure_connection.sendall(_encode(request) + b"\n")
                buffer = bytearray()
                subscribed = False
                pending: list[JsonObject] = []
                while True:
                    message = _read_json(secure_connection, buffer)
                    if message.get("id") == request_id:
                        result = _response_result(message)
                        history = _subscription_history(result)
                        yield from history
                        yield from pending
                        pending.clear()
                        subscribed = True
                    elif message.get("method") == "factory.notification":
                        params = message.get("params")
                        if isinstance(params, dict):
                            if subscribed:
                                yield params
                            else:
                                pending.append(params)
        except OSError as error:
            raise FactoryConnectionError(str(error)) from error

    def _take_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id


def _request(method: str, params: JsonParams | None, request_id: int) -> JsonObject:
    request: JsonObject = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params is not None:
        request["params"] = params
    return request


def _encode(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _read_json(connection: ssl.SSLSocket, buffer: bytearray) -> JsonObject:
    while b"\n" not in buffer:
        chunk = connection.recv(16_384)
        if not chunk:
            raise FactoryConnectionError("server closed the connection")
        buffer.extend(chunk)
        if len(buffer) > _MAX_MESSAGE_BYTES:
            raise FactoryConnectionError("server message exceeded size limit")
    line, _, remainder = buffer.partition(b"\n")
    if len(line) > _MAX_MESSAGE_BYTES:
        raise FactoryConnectionError("server message exceeded size limit")
    buffer.clear()
    buffer.extend(remainder)
    try:
        value: object = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise FactoryConnectionError("server returned invalid JSON") from error
    if not isinstance(value, dict):
        raise FactoryConnectionError("server returned a non-object response")
    return _json_object(cast(dict[object, object], value))


def _json_object(value: dict[object, object]) -> JsonObject:
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise FactoryConnectionError("server returned a non-string JSON key")
        result[key] = _json_value(item)
    return result


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise FactoryConnectionError("server returned a non-finite JSON number")
    if isinstance(value, list):
        return [_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return _json_object(cast(dict[object, object], value))
    raise FactoryConnectionError("server returned an unsupported JSON value")


def _response_result(response: JsonObject) -> JsonValue:
    error = response.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        data = error.get("data")
        detail = f": {data}" if data is not None else ""
        raise RemoteOperationError(f"{message}{detail}")
    if "result" not in response:
        raise FactoryConnectionError("server response has no result")
    return response["result"]


def _subscription_history(value: JsonValue) -> tuple[JsonObject, ...]:
    if not isinstance(value, dict):
        raise FactoryConnectionError("invalid subscription response")
    history = value.get("history")
    if not isinstance(history, list):
        raise FactoryConnectionError("subscription response has no history")
    events: list[JsonObject] = []
    for event in history:
        if not isinstance(event, dict):
            raise FactoryConnectionError("subscription history contains invalid event")
        events.append(event)
    return tuple(events)
