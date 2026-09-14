"""Behavior tests for the JSON-RPC protocol."""

import json

import pytest

from factory.jsonrpc import (
    DuplicateMethodError,
    InvalidMethodNameError,
    JsonParams,
    JsonRpcContext,
    JsonRpcProtocol,
    MethodFailure,
    MethodResult,
    MethodSuccess,
)


def _decode(response: bytes | None) -> object:
    assert response is not None
    return json.loads(response)


def _echo(params: JsonParams | None, context: JsonRpcContext) -> MethodResult:
    return MethodSuccess(params)


def _fail(params: JsonParams | None, context: JsonRpcContext) -> MethodResult:
    return MethodFailure(10, "expected failure", {"params": params})


def _crash(params: JsonParams | None, context: JsonRpcContext) -> MethodResult:
    raise RuntimeError(params)


@pytest.fixture
def protocol() -> JsonRpcProtocol:
    dispatcher = JsonRpcProtocol()
    dispatcher.register("echo", _echo)
    dispatcher.register("fail", _fail)
    dispatcher.register("crash", _crash)
    return dispatcher


@pytest.mark.parametrize("params", [None, [], [1, "two"], {}, {"value": 3}])
def test_dispatches_requests(
    protocol: JsonRpcProtocol,
    params: JsonParams | None,
) -> None:
    request: dict[str, object] = {"jsonrpc": "2.0", "method": "echo", "id": 7}
    if params is not None:
        request["params"] = params

    response = protocol.handle(json.dumps(request).encode())

    assert _decode(response) == {"jsonrpc": "2.0", "result": params, "id": 7}
    assert response is not None
    assert b" " not in response


def test_returns_expected_method_failure(protocol: JsonRpcProtocol) -> None:
    response = protocol.handle(
        b'{"jsonrpc":"2.0","method":"fail","params":[1],"id":"a"}'
    )

    assert _decode(response) == {
        "jsonrpc": "2.0",
        "error": {
            "code": 10,
            "message": "expected failure",
            "data": {"params": [1]},
        },
        "id": "a",
    }


@pytest.mark.parametrize("message", [b"{", b"\xff", b"NaN"])
def test_returns_parse_error_for_invalid_json(
    protocol: JsonRpcProtocol,
    message: bytes,
) -> None:
    assert _decode(protocol.handle(message)) == {
        "jsonrpc": "2.0",
        "error": {"code": -32700, "message": "Parse error"},
        "id": None,
    }


@pytest.mark.parametrize(
    "document",
    [
        None,
        1,
        {},
        {"jsonrpc": "1.0", "method": "echo", "id": 1},
        {"jsonrpc": "2.0", "method": 1, "id": 1},
        {"jsonrpc": "2.0", "method": "echo", "id": True},
    ],
)
def test_returns_invalid_request(protocol: JsonRpcProtocol, document: object) -> None:
    response = protocol.handle(json.dumps(document).encode())

    assert _decode(response) == {
        "jsonrpc": "2.0",
        "error": {"code": -32600, "message": "Invalid Request"},
        "id": None,
    }


def test_returns_invalid_params(protocol: JsonRpcProtocol) -> None:
    response = protocol.handle(b'{"jsonrpc":"2.0","method":"echo","params":1,"id":2}')

    assert _decode(response) == {
        "jsonrpc": "2.0",
        "error": {"code": -32602, "message": "Invalid params"},
        "id": 2,
    }


def test_returns_method_not_found(protocol: JsonRpcProtocol) -> None:
    response = protocol.handle(b'{"jsonrpc":"2.0","method":"missing","id":3}')

    assert _decode(response) == {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found"},
        "id": 3,
    }


def test_returns_internal_error(protocol: JsonRpcProtocol) -> None:
    response = protocol.handle(b'{"jsonrpc":"2.0","method":"crash","id":4}')

    assert _decode(response) == {
        "jsonrpc": "2.0",
        "error": {"code": -32603, "message": "Internal error"},
        "id": 4,
    }


def test_executes_notification_without_response(protocol: JsonRpcProtocol) -> None:
    seen: list[JsonParams | None] = []

    def record(
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        seen.append(params)
        return MethodSuccess(None)

    protocol.register("record", record)

    response = protocol.handle(
        b'{"jsonrpc":"2.0","method":"record","params":{"value":1}}'
    )

    assert response is None
    assert seen == [{"value": 1}]


def test_handles_mixed_batch(protocol: JsonRpcProtocol) -> None:
    response = protocol.handle(
        b'[{"jsonrpc":"2.0","method":"echo","params":[1],"id":1},'
        b'{"jsonrpc":"2.0","method":"echo","params":[2]},'
        b'{"jsonrpc":"2.0","method":"missing","id":2},1]'
    )

    assert _decode(response) == [
        {"jsonrpc": "2.0", "result": [1], "id": 1},
        {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found"},
            "id": 2,
        },
        {
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request"},
            "id": None,
        },
    ]


def test_returns_no_response_for_notification_batch(protocol: JsonRpcProtocol) -> None:
    response = protocol.handle(
        b'[{"jsonrpc":"2.0","method":"echo"},{"jsonrpc":"2.0","method":"missing"}]'
    )

    assert response is None


def test_rejects_empty_batch(protocol: JsonRpcProtocol) -> None:
    assert _decode(protocol.handle(b"[]")) == {
        "jsonrpc": "2.0",
        "error": {"code": -32600, "message": "Invalid Request"},
        "id": None,
    }


@pytest.mark.parametrize("name", ["", "rpc.reserved"])
def test_rejects_invalid_method_names(name: str) -> None:
    protocol = JsonRpcProtocol()

    with pytest.raises(InvalidMethodNameError):
        protocol.register(name, _echo)


def test_rejects_duplicate_method_names() -> None:
    protocol = JsonRpcProtocol()
    protocol.register("echo", _echo)

    with pytest.raises(DuplicateMethodError):
        protocol.register("echo", _echo)


def test_session_sends_notifications_and_runs_close_callbacks() -> None:
    protocol = JsonRpcProtocol()
    sent: list[bytes] = []
    closed: list[bool] = []

    def subscribe(
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        context.on_close(lambda: closed.append(True))
        context.notify("event", {"value": 1})
        return MethodSuccess(True)

    protocol.register("subscribe", subscribe)
    session = protocol.open(sent.append)

    response = session.handle(b'{"jsonrpc":"2.0","method":"subscribe","id":1}')
    session.close()
    session.close()

    assert _decode(response) == {"jsonrpc": "2.0", "result": True, "id": 1}
    assert sent == [b'{"jsonrpc":"2.0","method":"event","params":{"value":1}}']
    assert closed == [True]
