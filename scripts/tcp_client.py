"""Command-line client for the Factory TLS JSON-RPC server."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
from collections.abc import Sequence
from pathlib import Path

_MAX_RESPONSE_BYTES = 1_048_576


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hostname", help="server hostname used for TLS verification")
    parser.add_argument("certificate", type=Path, help="trusted PEM certificate")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--method", default="factory.state")
    parser.add_argument(
        "--params",
        help='JSON object or array, for example \'{"message":"hello"}\'',
    )
    parser.add_argument("--id", default="1", help="string request id")
    parser.add_argument(
        "--notification",
        action="store_true",
        help="send no id and do not wait for a response",
    )
    return parser.parse_args(arguments)


def _request_bytes(args: argparse.Namespace) -> bytes:
    request: dict[str, object] = {"jsonrpc": "2.0", "method": args.method}
    if not args.notification:
        request["id"] = args.id
    if args.params is not None:
        params: object = json.loads(args.params)
        if not isinstance(params, (dict, list)):
            raise argparse.ArgumentTypeError("--params must be a JSON object or array")
        request["params"] = params
    return json.dumps(request, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _receive_line(connection: ssl.SSLSocket) -> bytes:
    response = bytearray()
    while b"\n" not in response:
        chunk = connection.recv(16_384)
        if not chunk:
            raise ConnectionError("server closed the connection before responding")
        response.extend(chunk)
        if len(response) > _MAX_RESPONSE_BYTES:
            raise ConnectionError("server response exceeded size limit")
    line, _, _ = response.partition(b"\n")
    return bytes(line)


def main() -> None:
    """Send one JSON-RPC request and print its response."""
    args = _parse_args()
    request = _request_bytes(args)
    context = ssl.create_default_context(cafile=str(args.certificate))

    with (
        socket.create_connection((args.hostname, args.port)) as connection,
        context.wrap_socket(
            connection,
            server_hostname=args.hostname,
        ) as secure_connection,
    ):
        secure_connection.sendall(request + b"\n")
        if not args.notification:
            print(_receive_line(secure_connection).decode("utf-8"))


if __name__ == "__main__":
    main()
