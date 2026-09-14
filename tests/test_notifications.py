"""Behavior tests for durable and live Factory notifications."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from factory.notifications import Notification, NotificationLog, notification_json


class MemoryStream:
    def __init__(self, path: MemoryPath) -> None:
        self._path = path

    def __enter__(self) -> MemoryStream:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def write(self, value: str) -> int:
        self._path.content += value
        return len(value)

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        return 1


class MemoryPath:
    def __init__(self, content: str = "", *, exists: bool = True) -> None:
        self.content = content
        self._exists = exists

    @property
    def parent(self) -> MemoryPath:
        return self

    def mkdir(self, *, parents: bool, exist_ok: bool) -> None:
        return None

    def exists(self) -> bool:
        return self._exists

    def touch(self, *, mode: int) -> None:
        self._exists = True

    def read_text(self, *, encoding: str) -> str:
        return self.content

    def open(self, mode: str, *, encoding: str) -> MemoryStream:
        return MemoryStream(self)


def test_open_recovers_valid_events_and_repairs_partial_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import factory.notifications as notifications_module

    path = MemoryPath(
        '{"sequence":1,"timestamp":"t","event":"started","data":{}}\n{"sequence":'
    )
    repaired: list[str] = []

    def repair(target: Path, content: str) -> None:
        repaired.append(content)

    monkeypatch.setattr(
        notifications_module,
        "_repair_log",
        repair,
    )

    log = NotificationLog.open(cast(Path, path))

    assert [event.event for event in log.history()] == ["started"]
    assert repaired == ['{"sequence":1,"timestamp":"t","event":"started","data":{}}\n']


def test_publish_persists_before_notifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = MemoryPath()

    def fsync(descriptor: int) -> None:
        return None

    monkeypatch.setattr(os, "fsync", fsync)
    log = NotificationLog.open(cast(Path, path))
    seen: list[Notification] = []
    subscriber, history = log.subscribe(seen.append)

    event = log.publish("channel.created", {"channel": "@1"})

    assert history == ()
    assert event.sequence == 1
    assert seen == [event]
    assert '"event":"channel.created"' in path.content
    log.unsubscribe(subscriber)
    log.publish("later")
    assert seen == [event]


def test_subscribe_returns_history_after_sequence() -> None:
    path = MemoryPath()
    log = NotificationLog(
        cast(Path, path),
        [
            Notification(1, "t", "one", {}),
            Notification(2, "t", "two", {}),
        ],
    )

    subscriber, history = log.subscribe(lambda event: None, after=1)

    assert subscriber == 1
    assert [event.event for event in history] == ["two"]


def test_notification_json_exposes_ordering_metadata() -> None:
    assert notification_json(Notification(3, "time", "event", {"value": True})) == {
        "sequence": 3,
        "timestamp": "time",
        "event": "event",
        "data": {"value": True},
    }
