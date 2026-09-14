"""Behavior tests for Factory's JSON-RPC operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest

from factory.jsonrpc import JsonObject
from factory.notifications import Notification
from factory.service import FactoryService, InvalidMonitorIntervalError
from factory.tmux import (
    ChannelState,
    FactoryState,
    OperationResult,
    OperationSuccess,
    ProcessState,
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


def _service() -> tuple[FactoryService, FakeRuntime, FakeNotifications]:
    runtime = FakeRuntime()
    notifications = FakeNotifications()
    service = FactoryService.create(runtime, notifications, monitor_interval=60)  # type: ignore[arg-type]
    return service, runtime, notifications


def _call(
    service: FactoryService, method: str, params: JsonObject | None = None
) -> object:
    request: JsonObject = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        request["params"] = params
    response = service.protocol.handle(json.dumps(request).encode())
    assert response is not None
    return json.loads(response)["result"]


def test_create_rejects_invalid_monitor_interval() -> None:
    _, runtime, notifications = _service()

    with pytest.raises(InvalidMonitorIntervalError):
        FactoryService.create(runtime, notifications, monitor_interval=0)  # type: ignore[arg-type]


def test_start_recovers_session_and_publishes_state() -> None:
    service, _, notifications = _service()

    result = service.start()
    service.close()

    assert result == OperationSuccess(None)
    assert notifications.events[0].event == "factory.started"
    assert notifications.events[0].data["channel_count"] == 1
    assert notifications.events[-1].event == "factory.stopped"


def test_factory_state_is_read_live() -> None:
    service, _, _ = _service()

    result = _call(service, "factory.state")

    assert isinstance(result, dict)
    assert result["channel_count"] == 1
    assert result["process_count"] == 1
    assert result["channels"][0]["processes"][0]["command"] == "codex"


def test_channel_create_maps_to_tmux_and_publishes() -> None:
    service, runtime, notifications = _service()

    result = _call(service, "channel.create", {"name": "reviewer"})

    assert result == {"channel": "@2", "name": "reviewer"}
    assert runtime.created == ["reviewer"]
    assert notifications.events[-1].event == "channel.created"


def test_mailbox_send_and_read_map_to_tmux() -> None:
    service, runtime, _ = _service()

    sent = _call(
        service,
        "mailbox.send",
        {"channel": "@1", "message": "fix it"},
    )
    read = _call(service, "mailbox.read", {"channel": "@1", "lines": 50})

    assert sent == {"channel": "@1"}
    assert read == {"channel": "@1", "content": "pane output\n"}
    assert runtime.sent == [("@1", "fix it")]
    assert runtime.read == [("@1", 50)]


def test_notification_history_and_live_subscription() -> None:
    service, _, notifications = _service()
    notifications.publish("existing")
    sent: list[bytes] = []
    session = service.protocol.open(sent.append)

    response = session.handle(
        b'{"jsonrpc":"2.0","method":"notification.subscribe",'
        b'"params":{"after":0},"id":1}'
    )
    notifications.publish("live", {"value": 2})
    session.close()

    assert response is not None
    result = json.loads(response)["result"]
    assert result["history"][0]["event"] == "existing"
    assert json.loads(sent[0])["params"]["event"] == "live"
    assert notifications.subscribers == {}


def test_notification_list_resumes_after_sequence() -> None:
    service, _, notifications = _service()
    notifications.publish("one")
    notifications.publish("two")

    result = cast(
        list[JsonObject],
        _call(service, "notification.list", {"after": 1}),
    )

    assert [event["event"] for event in result] == ["two"]
