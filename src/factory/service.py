"""Factory operations exposed through JSON-RPC."""

from __future__ import annotations

import threading

from factory.jsonrpc import (
    JsonObject,
    JsonParams,
    JsonRpcContext,
    JsonRpcProtocol,
    JsonValue,
    MethodFailure,
    MethodResult,
    MethodSuccess,
)
from factory.notifications import Notification, NotificationLog, notification_json
from factory.tmux import (
    FactoryState,
    OperationFailure,
    OperationResult,
    OperationSuccess,
    TmuxRuntime,
)

_INVALID_PARAMS = MethodFailure(-32602, "Invalid params")


class FactoryServiceError(Exception):
    """Base class for Factory service configuration errors."""


class InvalidMonitorIntervalError(FactoryServiceError):
    """Raised when state monitoring is configured with an invalid interval."""


class FactoryService:
    """Coordinate tmux state, mailbox operations, and durable notifications."""

    def __init__(
        self,
        runtime: TmuxRuntime,
        notifications: NotificationLog,
        monitor_interval: float,
    ) -> None:
        self._runtime = runtime
        self._notifications = notifications
        self._monitor_interval = monitor_interval
        self._last_state: FactoryState | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._protocol = JsonRpcProtocol()
        self._register_methods()

    @classmethod
    def create(
        cls,
        runtime: TmuxRuntime,
        notifications: NotificationLog,
        *,
        monitor_interval: float = 1.0,
    ) -> FactoryService:
        """Create a service with a positive state-monitor interval."""
        if monitor_interval <= 0:
            raise InvalidMonitorIntervalError("monitor_interval must be positive")
        return cls(runtime, notifications, monitor_interval)

    @property
    def protocol(self) -> JsonRpcProtocol:
        """Return the JSON-RPC protocol configured with Factory methods."""
        return self._protocol

    def start(self) -> OperationResult[None]:
        """Recover the tmux session and begin observing external changes."""
        if self._monitor is not None:
            return OperationSuccess(None)
        ensured = self._runtime.ensure_session()
        if isinstance(ensured, OperationFailure):
            return ensured
        state = self._runtime.state()
        if isinstance(state, OperationFailure):
            return state
        self._last_state = state.value
        self._stop.clear()
        self._notifications.publish("factory.started", _state_json(state.value))
        self._monitor = threading.Thread(
            target=self._observe_state,
            name="factory-state-monitor",
            daemon=True,
        )
        self._monitor.start()
        return OperationSuccess(None)

    def close(self) -> None:
        """Stop state observation and publish a durable shutdown event."""
        monitor = self._monitor
        if monitor is None:
            return
        self._stop.set()
        monitor.join(timeout=self._monitor_interval + 1.0)
        self._monitor = None
        self._notifications.publish("factory.stopped")

    def _register_methods(self) -> None:
        self._protocol.register("factory.state", self._state)
        self._protocol.register("channel.create", self._create_channel)
        self._protocol.register("mailbox.send", self._send_message)
        self._protocol.register("mailbox.read", self._read_channel)
        self._protocol.register("notification.list", self._list_notifications)
        self._protocol.register("notification.subscribe", self._subscribe)

    def _state(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        if params is not None:
            return _INVALID_PARAMS
        state = self._runtime.state()
        if isinstance(state, OperationFailure):
            return _runtime_failure(state)
        return MethodSuccess(_state_json(state.value))

    def _create_channel(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        if not isinstance(params, dict) or set(params) != {"name"}:
            return _INVALID_PARAMS
        name = params["name"]
        if not isinstance(name, str):
            return _INVALID_PARAMS
        result = self._runtime.create_channel(name)
        if isinstance(result, OperationFailure):
            return _runtime_failure(result)
        data: JsonObject = {"channel": result.value, "name": name}
        self._notifications.publish("channel.created", data)
        return MethodSuccess(data)

    def _send_message(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        if not isinstance(params, dict) or set(params) != {"channel", "message"}:
            return _INVALID_PARAMS
        channel = params["channel"]
        message = params["message"]
        if not isinstance(channel, str) or not isinstance(message, str):
            return _INVALID_PARAMS
        result = self._runtime.send_message(channel, message)
        if isinstance(result, OperationFailure):
            return _runtime_failure(result)
        data: JsonObject = {"channel": channel}
        self._notifications.publish("mailbox.message_sent", data)
        return MethodSuccess(data)

    def _read_channel(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        if not isinstance(params, dict) or not set(params) <= {"channel", "lines"}:
            return _INVALID_PARAMS
        channel = params.get("channel")
        lines = params.get("lines", 200)
        if (
            not isinstance(channel, str)
            or not isinstance(lines, int)
            or isinstance(lines, bool)
        ):
            return _INVALID_PARAMS
        result = self._runtime.read_channel(channel, lines)
        if isinstance(result, OperationFailure):
            return _runtime_failure(result)
        return MethodSuccess({"channel": channel, "content": result.value})

    def _list_notifications(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        after = _after_sequence(params)
        if after is None:
            return _INVALID_PARAMS
        events = self._notifications.history(after)
        return MethodSuccess([notification_json(event) for event in events])

    def _subscribe(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        after = _after_sequence(params)
        if after is None:
            return _INVALID_PARAMS

        def send(notification: Notification) -> None:
            context.notify("factory.notification", notification_json(notification))

        subscriber_id, history = self._notifications.subscribe(send, after=after)
        context.on_close(lambda: self._notifications.unsubscribe(subscriber_id))
        return MethodSuccess(
            {
                "subscribed": True,
                "history": [notification_json(event) for event in history],
            }
        )

    def _observe_state(self) -> None:
        while not self._stop.wait(self._monitor_interval):
            result = self._runtime.state()
            if self._stop.is_set():
                return
            if isinstance(result, OperationFailure):
                if result.message != self._last_error:
                    self._notifications.publish(
                        "runtime.error",
                        {"message": result.message},
                    )
                    self._last_error = result.message
                continue
            self._last_error = None
            if _state_fingerprint(result.value) != _state_fingerprint(self._last_state):
                self._last_state = result.value
                self._notifications.publish("state.changed", _state_json(result.value))


def _after_sequence(params: JsonParams | None) -> int | None:
    if params is None:
        return 0
    if not isinstance(params, dict) or not set(params) <= {"after"}:
        return None
    after = params.get("after", 0)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        return None
    return after


def _runtime_failure(failure: OperationFailure) -> MethodFailure:
    return MethodFailure(
        -32000, "Runtime operation failed", {"message": failure.message}
    )


def _state_json(state: FactoryState) -> JsonObject:
    channels: list[JsonValue] = []
    for channel in state.channels:
        processes: list[JsonValue] = [
            {
                "pane_id": process.pane_id,
                "pane_index": process.pane_index,
                "pid": process.pid,
                "command": process.command,
                "status": process.status,
                "active": process.active,
                "title": process.title,
            }
            for process in channel.processes
        ]
        channels.append(
            {
                "id": channel.channel_id,
                "name": channel.name,
                "active": channel.active,
                "processes": processes,
            }
        )
    return {
        "session": state.session,
        "channel_count": state.channel_count,
        "process_count": state.process_count,
        "channels": channels,
    }


def _state_fingerprint(state: FactoryState | None) -> tuple[object, ...] | None:
    if state is None:
        return None
    return tuple(
        (
            channel.channel_id,
            channel.name,
            channel.active,
            tuple(
                (
                    process.pane_id,
                    process.pane_index,
                    process.pid,
                    process.command,
                    process.status,
                    process.active,
                )
                for process in channel.processes
            ),
        )
        for channel in state.channels
    )
