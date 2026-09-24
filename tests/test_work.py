"""Behavior tests for Factory's work-unit system."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest

import factory.work as work_module
from factory.jsonrpc import JsonObject
from factory.notifications import Notification
from factory.tmux import (
    ChannelState,
    FactoryState,
    OperationFailure,
    OperationResult,
    OperationSuccess,
    ProcessState,
)
from factory.work import (
    DuplicateWorkUnitError,
    InvalidMonitorIntervalError,
    WorkContext,
    WorkFailure,
    WorkResult,
    WorkRunner,
    WorkSuccess,
    WorkUnit,
    load_work_units,
)


def _state() -> FactoryState:
    process = ProcessState("%1", 0, 123, "codex", "running", True, "agent")
    channel = ChannelState("@1", "worker", True, (process,))
    return FactoryState("factory", (channel,))


class FakeRuntime:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.read: list[tuple[str, int]] = []
        self.commands: list[tuple[str, ...]] = []

    def command(self, *arguments: str) -> OperationResult[str]:
        self.commands.append(arguments)
        return OperationSuccess("3.6\n")

    def ensure_session(self) -> OperationResult[None]:
        return OperationSuccess(None)

    def state(self) -> OperationResult[FactoryState]:
        return OperationSuccess(_state())

    def create_channel(self, name: str) -> OperationResult[str]:
        self.created.append(name)
        return OperationSuccess("@2")

    def send_message(self, channel: str, message: str) -> OperationResult[None]:
        self.sent.append((channel, message))
        return OperationSuccess(None)

    def read_channel(self, channel: str, lines: int) -> OperationResult[str]:
        self.read.append((channel, lines))
        return OperationSuccess("pane output\n")


class FakeNotifications:
    def __init__(self) -> None:
        self.events: list[Notification] = []
        self.subscribers: dict[int, Callable[[Notification], None]] = {}

    def publish(self, event: str, data: JsonObject | None = None) -> Notification:
        notification = Notification(
            len(self.events) + 1,
            "time",
            event,
            data or {},
        )
        self.events.append(notification)
        for subscriber in tuple(self.subscribers.values()):
            subscriber(notification)
        return notification

    def history(self, after: int = 0) -> tuple[Notification, ...]:
        return tuple(event for event in self.events if event.sequence > after)

    def subscribe(
        self,
        subscriber: Callable[[Notification], None],
        *,
        after: int = 0,
    ) -> tuple[int, tuple[Notification, ...]]:
        token = len(self.subscribers) + 1
        self.subscribers[token] = subscriber
        return token, self.history(after)

    def unsubscribe(self, subscriber_id: int) -> None:
        self.subscribers.pop(subscriber_id, None)


def _runner(*units: WorkUnit) -> tuple[WorkRunner, FakeRuntime, FakeNotifications]:
    runtime = FakeRuntime()
    notifications = FakeNotifications()
    runner = WorkRunner.create(
        runtime,  # pyright: ignore[reportArgumentType]
        notifications,  # pyright: ignore[reportArgumentType]
        units=units,
        monitor_interval=60,
    )
    return runner, runtime, notifications


def _call(runner: WorkRunner, unit: str, input: JsonObject | None = None) -> object:
    request: JsonObject = {
        "jsonrpc": "2.0",
        "method": "work.run",
        "params": {"unit": unit, "input": input or {}},
        "id": 1,
    }
    response = runner.protocol.handle(json.dumps(request).encode())
    assert response is not None
    return json.loads(response)["result"]


def test_create_rejects_invalid_monitor_interval() -> None:
    _, runtime, notifications = _runner()

    with pytest.raises(InvalidMonitorIntervalError):
        WorkRunner.create(runtime, notifications, monitor_interval=0)  # type: ignore[arg-type]


def test_start_recovers_session_and_publishes_state() -> None:
    runner, _, notifications = _runner()

    result = runner.start()
    runner.close()

    assert result == OperationSuccess(None)
    assert notifications.events[0].event == "factory.started"
    assert notifications.events[0].data["channel_count"] == 1
    assert notifications.events[-1].event == "factory.stopped"


def test_factory_state_is_read_live() -> None:
    runner, _, _ = _runner()

    result = _call(runner, "factory.state")

    assert isinstance(result, dict)
    assert result["channel_count"] == 1
    assert result["process_count"] == 1
    assert result["channels"][0]["processes"][0]["command"] == "codex"


def test_channel_create_maps_to_tmux_and_publishes() -> None:
    runner, runtime, notifications = _runner()

    result = _call(runner, "channel.create", {"name": "reviewer"})

    assert result == {"channel": "@2", "name": "reviewer"}
    assert runtime.created == ["reviewer"]
    assert notifications.events[-1].event == "channel.created"


def test_mailbox_send_and_read_map_to_tmux() -> None:
    runner, runtime, _ = _runner()

    sent = _call(
        runner,
        "mailbox.send",
        {"channel": "@1", "message": "fix it"},
    )
    read = _call(runner, "mailbox.read", {"channel": "@1", "lines": 50})

    assert sent == {"channel": "@1"}
    assert read == {"channel": "@1", "content": "pane output\n"}
    assert runtime.sent == [("@1", "fix it")]
    assert runtime.read == [("@1", 50)]


def test_notification_history_and_live_subscription() -> None:
    runner, _, notifications = _runner()
    notifications.publish("existing")
    sent: list[bytes] = []
    session = runner.protocol.open(sent.append)

    response = session.handle(
        b'{"jsonrpc":"2.0","method":"work.run","params":'
        b'{"unit":"notification.subscribe","input":{"after":0}},"id":1}'
    )
    notifications.publish("live", {"value": 2})
    session.close()

    assert response is not None
    result = json.loads(response)["result"]
    assert result["history"][0]["event"] == "existing"
    assert json.loads(sent[0])["params"]["event"] == "live"
    assert notifications.subscribers == {}


def test_notification_list_resumes_after_sequence() -> None:
    runner, _, notifications = _runner()
    notifications.publish("one")
    notifications.publish("two")

    result = cast(
        list[JsonObject],
        _call(runner, "notification.list", {"after": 1}),
    )

    assert [event["event"] for event in result] == ["two"]


def test_plugin_composes_builtins_through_context() -> None:
    class CreateReviewer:
        name = "review.create"

        def run(self, input: JsonObject, context: WorkContext) -> WorkResult:
            return context.run("channel.create", input)

    runner, runtime, _ = _runner(CreateReviewer())

    result = _call(runner, "review.create", {"name": "reviewer"})
    response = runner.protocol.handle(b'{"jsonrpc":"2.0","method":"work.list","id":1}')

    assert result == {"channel": "@2", "name": "reviewer"}
    assert runtime.created == ["reviewer"]
    assert response is not None
    assert "review.create" in json.loads(response)["result"]
    with pytest.raises(DuplicateWorkUnitError):
        _runner(CreateReviewer(), CreateReviewer())


def test_plugin_reaches_raw_tmux_through_context() -> None:
    class TmuxProbe:
        name = "tmux.probe"

        def run(self, input: JsonObject, context: WorkContext) -> WorkResult:
            result = context.tmux.command("display-message", "-p", "#{version}")
            if isinstance(result, OperationFailure):
                return WorkFailure(-32000, result.message)
            return WorkSuccess(result.value)

    runner, runtime, _ = _runner(TmuxProbe())

    result = _call(runner, "tmux.probe")

    assert result == "3.6\n"
    assert runtime.commands == [("display-message", "-p", "#{version}")]


def test_load_work_units_discovers_entry_point_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Plugin:
        name = "plugin.example"

        def run(self, input: JsonObject, context: WorkContext) -> WorkResult:
            return context.run("factory.state", {})

    class EntryPoint:
        name = "example"

        def load(self) -> object:
            return Plugin

    def discover(*, group: str) -> tuple[EntryPoint, ...]:
        assert group == "factory.plugins"
        return (EntryPoint(),)

    monkeypatch.setattr(work_module, "entry_points", discover)

    assert [unit.name for unit in load_work_units()] == ["plugin.example"]
