"""Factory's single programmable execution abstraction."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

from factory.jsonrpc import (
    JsonObject,
    JsonParams,
    JsonRpcContext,
    JsonRpcMethod,
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


class WorkError(Exception):
    """Base class for work-system configuration errors."""


class InvalidMonitorIntervalError(WorkError):
    """Raised when state monitoring is configured with an invalid interval."""


class InvalidWorkUnitError(WorkError):
    """Raised when a work unit has an invalid or reserved name."""


class DuplicateWorkUnitError(WorkError):
    """Raised when a work unit name is registered more than once."""


WorkSuccess = MethodSuccess
WorkFailure = MethodFailure
type WorkResult = MethodResult


@runtime_checkable
class WorkUnit(Protocol):
    """One named, composable Factory operation."""

    @property
    def name(self) -> str:
        """Return the globally unique work-unit name."""
        ...

    def run(self, input: JsonObject, context: WorkContext) -> WorkResult:
        """Execute with JSON input and Factory's controlled context."""
        ...


type WorkExecutor = Callable[[str, JsonObject, WorkContext], WorkResult]


class WorkRegistry:
    """Own the validated set of available work units."""

    def __init__(self) -> None:
        self._units: dict[str, WorkUnit] = {}

    def register(self, unit: WorkUnit) -> None:
        """Register a uniquely named work unit."""
        if not unit.name or unit.name.startswith("work."):
            raise InvalidWorkUnitError(unit.name)
        if unit.name in self._units:
            raise DuplicateWorkUnitError(unit.name)
        self._units[unit.name] = unit

    def names(self) -> tuple[str, ...]:
        """Return registered names in deterministic order."""
        return tuple(sorted(self._units))

    def get(self, name: str) -> WorkUnit | None:
        """Return a registered unit by name."""
        return self._units.get(name)


class WorkContext:
    """The controlled Factory surface available to work units."""

    def __init__(
        self,
        executor: WorkExecutor,
        connection: JsonRpcContext,
    ) -> None:
        self._executor = executor
        self._connection = connection

    def run(self, unit: str, input: JsonObject) -> WorkResult:
        """Compose another work unit through the same execution path."""
        return self._executor(unit, input, self)


@dataclass(frozen=True, slots=True)
class _FunctionWorkUnit:
    name: str
    function: JsonRpcMethod

    def run(self, input: JsonObject, context: WorkContext) -> WorkResult:
        return self.function(
            input or None,
            context._connection,  # pyright: ignore[reportPrivateUsage]
        )


def load_work_units() -> tuple[WorkUnit, ...]:
    """Load user-defined units from the ``factory.plugins`` entry-point group."""
    units: list[WorkUnit] = []
    for entry_point in entry_points(group="factory.plugins"):
        candidate: object = entry_point.load()
        if isinstance(candidate, type):
            candidate = candidate()
        if not isinstance(candidate, WorkUnit):
            raise InvalidWorkUnitError(entry_point.name)
        units.append(candidate)
    return tuple(units)


class WorkRunner:
    """Run all built-in and user-defined Factory behavior."""

    def __init__(
        self,
        runtime: TmuxRuntime,
        notifications: NotificationLog,
        monitor_interval: float,
        registry: WorkRegistry,
    ) -> None:
        self._runtime = runtime
        self._notifications = notifications
        self._monitor_interval = monitor_interval
        self._last_state: FactoryState | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._registry = registry
        self._protocol = JsonRpcProtocol()
        self._protocol.register("work.run", self._run)
        self._protocol.register("work.list", self._list)

    @classmethod
    def create(
        cls,
        runtime: TmuxRuntime,
        notifications: NotificationLog,
        *,
        units: Iterable[WorkUnit] = (),
        monitor_interval: float = 1.0,
    ) -> WorkRunner:
        """Create a runner with built-in and user-defined work units."""
        if monitor_interval <= 0:
            raise InvalidMonitorIntervalError("monitor_interval must be positive")
        registry = WorkRegistry()
        runner = cls(runtime, notifications, monitor_interval, registry)
        runner._register_builtins()
        for unit in units:
            registry.register(unit)
        return runner

    @property
    def protocol(self) -> JsonRpcProtocol:
        """Return the generic work JSON-RPC adapter."""
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

    def _register_builtins(self) -> None:
        for name, function in (
            ("factory.state", self._state),
            ("channel.create", self._create_channel),
            ("mailbox.send", self._send_message),
            ("mailbox.read", self._read_channel),
            ("notification.list", self._list_notifications),
            ("notification.subscribe", self._subscribe),
        ):
            self._registry.register(_FunctionWorkUnit(name, function))

    def _run(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        if not isinstance(params, dict) or set(params) != {"unit", "input"}:
            return _INVALID_PARAMS
        unit = params["unit"]
        input = params["input"]
        if not isinstance(unit, str) or not isinstance(input, dict):
            return _INVALID_PARAMS
        work_context = WorkContext(self._execute, context)
        return self._execute(unit, input, work_context)

    def _execute(
        self,
        unit: str,
        input: JsonObject,
        context: WorkContext,
    ) -> WorkResult:
        work = self._registry.get(unit)
        if work is None:
            return WorkFailure(-32601, "Work unit not found", {"unit": unit})
        return work.run(input, context)

    def _list(
        self,
        params: JsonParams | None,
        context: JsonRpcContext,
    ) -> MethodResult:
        if params is not None:
            return _INVALID_PARAMS
        return MethodSuccess(list(self._registry.names()))

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
