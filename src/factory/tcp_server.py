"""Concurrent, line-framed TLS TCP transport for any message protocol."""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

_LOGGER = logging.getLogger(__name__)
type ClientAddress = tuple[str, int]


class MessageSession(Protocol):
    """A protocol session scoped to one client connection."""

    def handle(self, message: bytes) -> bytes | None:
        """Handle one complete message and optionally return a response."""
        ...

    def close(self) -> None:
        """Release resources associated with the connection."""
        ...


class MessageProtocol(Protocol):
    """A protocol that creates an independent session per connection."""

    def open(self, sender: Callable[[bytes], None]) -> MessageSession:
        """Create a session whose sender can push unilateral messages."""
        ...


class TcpServerError(Exception):
    """Base class for TLS TCP server configuration errors."""


class InvalidServerConfigError(TcpServerError):
    """Raised when server configuration is outside its valid range."""


class CertificateLoadError(TcpServerError):
    """Raised when the TLS certificate or private key cannot be loaded."""


class SslTcpServer:
    """Serve newline-framed messages concurrently over TLS TCP connections."""

    def __init__(
        self,
        *,
        context: ssl.SSLContext,
        protocol: MessageProtocol,
        host: str,
        port: int,
        backlog: int,
        max_message_bytes: int,
    ) -> None:
        self._context = context
        self._protocol = protocol
        self._host = host
        self._port = port
        self._backlog = backlog
        self._max_message_bytes = max_message_bytes
        self._listener: socket.socket | None = None
        self._connections: set[ssl.SSLSocket] = set()
        self._threads: set[threading.Thread] = set()
        self._state_lock = threading.Lock()
        self._stopped = threading.Event()

    @classmethod
    def create(
        cls,
        *,
        context: ssl.SSLContext,
        protocol: MessageProtocol,
        host: str = "127.0.0.1",
        port: int = 8443,
        backlog: int = 128,
        max_message_bytes: int = 1_048_576,
    ) -> SslTcpServer:
        """Validate configuration and create a server without doing I/O."""
        if not host:
            raise InvalidServerConfigError("host must not be empty")
        if not 0 <= port <= 65_535:
            raise InvalidServerConfigError("port must be between 0 and 65535")
        if backlog < 1:
            raise InvalidServerConfigError("backlog must be positive")
        if max_message_bytes < 1:
            raise InvalidServerConfigError("max_message_bytes must be positive")
        return cls(
            context=context,
            protocol=protocol,
            host=host,
            port=port,
            backlog=backlog,
            max_message_bytes=max_message_bytes,
        )

    @property
    def address(self) -> tuple[str, int]:
        """Return the configured bind address."""
        return self._host, self._port

    def serve_forever(self) -> None:
        """Accept clients until ``close`` is called."""
        self._stopped.clear()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(self.address)
            listener.listen(self._backlog)
            with self._state_lock:
                self._listener = listener
            _LOGGER.info("TLS server listening on %s:%d", *self.address)
            try:
                self._accept_connections(listener)
            finally:
                self.close()
                with self._state_lock:
                    self._listener = None
                self._join_clients()

    def close(self) -> None:
        """Stop accepting clients and close every active connection."""
        self._stopped.set()
        with self._state_lock:
            listener = self._listener
            connections = tuple(self._connections)
        if listener is not None:
            listener.close()
        for connection in connections:
            connection.close()

    def _accept_connections(self, listener: socket.socket) -> None:
        while not self._stopped.is_set():
            try:
                connection, address = listener.accept()
            except OSError:
                if self._stopped.is_set():
                    return
                raise
            thread = threading.Thread(
                target=self._serve_client,
                args=(connection, cast(ClientAddress, address)),
                name=f"factory-client-{address}",
                daemon=True,
            )
            with self._state_lock:
                self._threads.add(thread)
            thread.start()
            self._discard_finished_threads()

    def _serve_client(
        self,
        connection: socket.socket,
        address: ClientAddress,
    ) -> None:
        current_thread = threading.current_thread()
        try:
            secure_connection = self._context.wrap_socket(
                connection,
                server_side=True,
            )
        except OSError:
            if not self._stopped.is_set():
                _LOGGER.warning("TLS handshake failed for %s", address)
            connection.close()
            self._forget_thread(current_thread)
            return

        send_lock = threading.Lock()

        def send(message: bytes) -> None:
            with send_lock:
                secure_connection.sendall(message + b"\n")

        session = self._protocol.open(send)
        with self._state_lock:
            self._connections.add(secure_connection)
        try:
            with secure_connection:
                _LOGGER.info("client connected: %s:%d", *address)
                self._read_messages(secure_connection, session, send)
        except OSError:
            if not self._stopped.is_set():
                _LOGGER.info("client disconnected unexpectedly: %s:%d", *address)
        except Exception:
            _LOGGER.exception("client handler failed for %s:%d", *address)
        finally:
            session.close()
            with self._state_lock:
                self._connections.discard(secure_connection)
            connection.close()
            self._forget_thread(current_thread)

    def _read_messages(
        self,
        connection: ssl.SSLSocket,
        session: MessageSession,
        send: Callable[[bytes], None],
    ) -> None:
        buffer = bytearray()
        while not self._stopped.is_set():
            chunk = connection.recv(16_384)
            if not chunk:
                return
            buffer.extend(chunk)
            while b"\n" in buffer:
                message, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if len(message) > self._max_message_bytes:
                    _LOGGER.warning("client message exceeded size limit")
                    return
                if not message:
                    continue
                response = session.handle(bytes(message))
                if response is not None:
                    send(response)
            if len(buffer) > self._max_message_bytes:
                _LOGGER.warning("client message exceeded size limit")
                return

    def _forget_thread(self, thread: threading.Thread) -> None:
        with self._state_lock:
            self._threads.discard(thread)

    def _discard_finished_threads(self) -> None:
        with self._state_lock:
            self._threads = {thread for thread in self._threads if thread.is_alive()}

    def _join_clients(self) -> None:
        with self._state_lock:
            threads = tuple(self._threads)
        for thread in threads:
            thread.join(timeout=2.0)


def create_server_context(
    certificate_path: Path,
    private_key_path: Path,
) -> ssl.SSLContext:
    """Load a TLS server context from a PEM certificate and private key."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certificate_path, private_key_path)
    except (OSError, ssl.SSLError) as error:
        raise CertificateLoadError(
            f"failed to load certificate {certificate_path} and key {private_key_path}"
        ) from error
    return context
