"""Compact JSON-RPC 2.0 parsing and dispatch."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)
type JsonObject = dict[str, JsonValue]
type JsonParams = list[JsonValue] | JsonObject
type RequestId = int | float | str | None
type MessageSender = Callable[[bytes], None]
_LOGGER = logging.getLogger(__name__)


class JsonRpcError(Exception):
    """Base class for JSON-RPC configuration errors."""


class InvalidMethodNameError(JsonRpcError):
    """Raised when a method is registered with an invalid name."""


class DuplicateMethodError(JsonRpcError):
    """Raised when a method name is registered more than once."""


@dataclass(frozen=True, slots=True)
class MethodSuccess:
    """A successful result returned by a JSON-RPC method."""

    result: JsonValue


@dataclass(frozen=True, slots=True)
class MethodFailure:
    """An expected failure returned by a JSON-RPC method."""

    code: int
    message: str
    data: JsonValue = None


type MethodResult = MethodSuccess | MethodFailure


class JsonRpcMethod(Protocol):
    """The callable interface required by the dispatcher."""

    def __call__(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        """Run a method using its request parameters and connection context."""
        ...


class JsonRpcContext:
    """Allow a method to notify its client and clean up on disconnect."""

    def __init__(self, sender: MessageSender) -> None:
        self._sender = sender
        self._close_callbacks: list[Callable[[], None]] = []
        self._closed = False

    def notify(self, method: str, params: JsonParams | None = None) -> None:
        """Send one JSON-RPC notification to the connected client."""
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._sender(_encode(message))

    def on_close(self, callback: Callable[[], None]) -> None:
        """Register cleanup work for when the client disconnects."""
        if self._closed:
            callback()
            return
        self._close_callbacks.append(callback)

    def close(self) -> None:
        """Run connection cleanup exactly once."""
        if self._closed:
            return
        self._closed = True
        callbacks = tuple(reversed(self._close_callbacks))
        self._close_callbacks.clear()
        for callback in callbacks:
            callback()


class JsonRpcSession:
    """Dispatch JSON-RPC messages for one client connection."""

    def __init__(
        self,
        methods: dict[str, JsonRpcMethod],
        sender: MessageSender,
    ) -> None:
        self._methods = methods
        self._context = JsonRpcContext(sender)

    def handle(self, message: bytes) -> bytes | None:
        """Return a compact response, or ``None`` for notifications."""
        document = _decode_json(message)
        if isinstance(document, _InvalidJson):
            return _encode(_error_response(None, -32700, "Parse error"))

        value = document.value
        if isinstance(value, list):
            return self._handle_batch(value)

        response = self._dispatch(value)
        return None if response is None else _encode(response)

    def close(self) -> None:
        """Release resources registered by methods in this session."""
        self._context.close()

    def _handle_batch(self, requests: list[JsonValue]) -> bytes | None:
        if not requests:
            return _encode(_error_response(None, -32600, "Invalid Request"))

        responses: list[JsonValue] = []
        for request in requests:
            response = self._dispatch(request)
            if response is not None:
                responses.append(response)
        return None if not responses else _encode(responses)

    def _dispatch(self, value: JsonValue) -> JsonObject | None:
        request = _parse_request(value)
        if isinstance(request, _InvalidRequest):
            return _error_response(None, -32600, "Invalid Request")

        response_id = None if request.is_notification else request.request_id
        if isinstance(request.params, _InvalidParams):
            return (
                None
                if request.is_notification
                else _error_response(response_id, -32602, "Invalid params")
            )

        method = self._methods.get(request.method)
        if method is None:
            return (
                None
                if request.is_notification
                else _error_response(response_id, -32601, "Method not found")
            )

        try:
            result = method(request.params, self._context)
        except Exception:
            _LOGGER.exception("JSON-RPC method failed: %s", request.method)
            return (
                None
                if request.is_notification
                else _error_response(response_id, -32603, "Internal error")
            )

        if request.is_notification:
            return None
        if isinstance(result, MethodFailure):
            return _error_response(
                response_id,
                result.code,
                result.message,
                data=result.data,
            )
        return {"jsonrpc": "2.0", "result": result.result, "id": response_id}


class JsonRpcProtocol:
    """Own a JSON-RPC method registry and create connection sessions."""

    def __init__(self) -> None:
        self._methods: dict[str, JsonRpcMethod] = {}

    def register(self, name: str, method: JsonRpcMethod) -> None:
        """Register one method under a non-empty, non-reserved name."""
        if not name or name.startswith("rpc."):
            raise InvalidMethodNameError(name)
        if name in self._methods:
            raise DuplicateMethodError(name)
        self._methods[name] = method

    def open(self, sender: MessageSender) -> JsonRpcSession:
        """Open a session for one client connection."""
        return JsonRpcSession(self._methods, sender)

    def handle(self, message: bytes) -> bytes | None:
        """Handle a standalone message without a persistent connection."""
        session = self.open(lambda notification: None)
        try:
            return session.handle(message)
        finally:
            session.close()


@dataclass(frozen=True, slots=True)
class _ValidJson:
    value: JsonValue


@dataclass(frozen=True, slots=True)
class _InvalidJson:
    pass


type _JsonValidation = _ValidJson | _InvalidJson
_INVALID_JSON = _InvalidJson()


@dataclass(frozen=True, slots=True)
class _InvalidParams:
    pass


_INVALID_PARAMS = _InvalidParams()
type _ParsedParams = JsonParams | _InvalidParams | None


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    params: _ParsedParams
    request_id: RequestId
    is_notification: bool


@dataclass(frozen=True, slots=True)
class _InvalidRequest:
    pass


type _RequestParse = _Request | _InvalidRequest
_INVALID_REQUEST = _InvalidRequest()


def _parse_request(value: JsonValue) -> _RequestParse:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        return _INVALID_REQUEST
    method = value.get("method")
    if not isinstance(method, str):
        return _INVALID_REQUEST

    is_notification = "id" not in value
    parsed_id = _request_id(value.get("id"))
    if not is_notification and isinstance(parsed_id, _InvalidId):
        return _INVALID_REQUEST
    request_id: RequestId = (
        None if is_notification or isinstance(parsed_id, _InvalidId) else parsed_id
    )
    params = _request_params(value.get("params"), present="params" in value)
    return _Request(method, params, request_id, is_notification)


@dataclass(frozen=True, slots=True)
class _InvalidId:
    pass


_INVALID_ID = _InvalidId()
type _ParsedId = RequestId | _InvalidId


def _request_id(value: JsonValue) -> _ParsedId:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return _INVALID_ID


def _request_params(value: JsonValue, *, present: bool) -> _ParsedParams:
    if not present:
        return None
    return value if isinstance(value, (dict, list)) else _INVALID_PARAMS


def _decode_json(message: bytes) -> _JsonValidation:
    try:
        raw: object = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _INVALID_JSON
    return _validate_json(raw)


def _validate_json(value: object) -> _JsonValidation:
    if value is None or isinstance(value, (bool, int, str)):
        return _ValidJson(value)
    if isinstance(value, float):
        return _ValidJson(value) if math.isfinite(value) else _INVALID_JSON
    if isinstance(value, list):
        items: list[JsonValue] = []
        for item in cast(list[object], value):
            validated = _validate_json(item)
            if isinstance(validated, _InvalidJson):
                return _INVALID_JSON
            items.append(validated.value)
        return _ValidJson(items)
    if isinstance(value, dict):
        fields: JsonObject = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                return _INVALID_JSON
            validated = _validate_json(item)
            if isinstance(validated, _InvalidJson):
                return _INVALID_JSON
            fields[key] = validated.value
        return _ValidJson(fields)
    return _INVALID_JSON


def _error_response(
    request_id: RequestId,
    code: int,
    message: str,
    *,
    data: JsonValue = None,
) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "error": error, "id": request_id}


def _encode(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
