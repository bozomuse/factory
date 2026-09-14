"""Persistent Factory events and live notification subscriptions."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from factory.jsonrpc import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class Notification:
    """One durable, ordered Factory event."""

    sequence: int
    timestamp: str
    event: str
    data: JsonObject


type NotificationSubscriber = Callable[[Notification], None]


class NotificationLog:
    """Append durable events and fan them out to connected subscribers."""

    def __init__(self, path: Path, history: list[Notification]) -> None:
        self._path = path
        self._history = history
        self._subscribers: dict[int, NotificationSubscriber] = {}
        self._next_subscriber = 1
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path) -> NotificationLog:
        """Open a log, recovering every complete valid event on disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
            return cls(path, [])

        content = path.read_text(encoding="utf-8")
        history: list[Notification] = []
        for line in content.splitlines():
            notification = _decode_notification(line)
            if notification is not None:
                history.append(notification)
        history = list({item.sequence: item for item in history}.values())
        history.sort(key=lambda item: item.sequence)
        canonical = "".join(_encode_notification(item) + "\n" for item in history)
        if content != canonical:
            _repair_log(path, canonical)
        return cls(path, history)

    def publish(self, event: str, data: JsonObject | None = None) -> Notification:
        """Persist an event before delivering it to live subscribers."""
        with self._lock:
            sequence = self._history[-1].sequence + 1 if self._history else 1
            notification = Notification(
                sequence=sequence,
                timestamp=datetime.now(UTC).isoformat(),
                event=event,
                data={} if data is None else data,
            )
            encoded = _encode_notification(notification)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._history.append(notification)
            subscribers = tuple(self._subscribers.items())

        stale: list[int] = []
        for subscriber_id, subscriber in subscribers:
            try:
                subscriber(notification)
            except OSError:
                stale.append(subscriber_id)
        for subscriber_id in stale:
            self.unsubscribe(subscriber_id)
        return notification

    def history(self, after: int = 0) -> tuple[Notification, ...]:
        """Return durable events with sequence numbers greater than ``after``."""
        with self._lock:
            return tuple(item for item in self._history if item.sequence > after)

    def subscribe(
        self,
        subscriber: NotificationSubscriber,
        *,
        after: int = 0,
    ) -> tuple[int, tuple[Notification, ...]]:
        """Subscribe and atomically return events newer than ``after``."""
        with self._lock:
            subscriber_id = self._next_subscriber
            self._next_subscriber += 1
            self._subscribers[subscriber_id] = subscriber
            history = tuple(item for item in self._history if item.sequence > after)
            return subscriber_id, history

    def unsubscribe(self, subscriber_id: int) -> None:
        """Remove a subscription; unknown tokens are harmless."""
        with self._lock:
            self._subscribers.pop(subscriber_id, None)


def notification_json(notification: Notification) -> JsonObject:
    """Convert a notification to its public JSON representation."""
    return {
        "sequence": notification.sequence,
        "timestamp": notification.timestamp,
        "event": notification.event,
        "data": notification.data,
    }


def _encode_notification(notification: Notification) -> str:
    return json.dumps(
        notification_json(notification),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _repair_log(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_notification(line: str) -> Notification | None:
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    fields = cast(dict[object, object], value)
    sequence = fields.get("sequence")
    timestamp = fields.get("timestamp")
    event = fields.get("event")
    data = fields.get("data")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not isinstance(timestamp, str)
        or not isinstance(event, str)
        or not event
        or not isinstance(data, dict)
    ):
        return None
    json_data = _json_object(cast(dict[object, object], data))
    if json_data is None:
        return None
    return Notification(sequence, timestamp, event, json_data)


def _json_object(value: dict[object, object]) -> JsonObject | None:
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        converted = _json_value(item)
        if isinstance(converted, _InvalidJson):
            return None
        result[key] = converted.value
    return result


@dataclass(frozen=True, slots=True)
class _ValidJson:
    value: JsonValue


@dataclass(frozen=True, slots=True)
class _InvalidJson:
    pass


type _JsonResult = _ValidJson | _InvalidJson
_INVALID_JSON = _InvalidJson()


def _json_value(value: object) -> _JsonResult:
    if value is None or isinstance(value, (bool, int, str)):
        return _ValidJson(value)
    if isinstance(value, float):
        return _ValidJson(value) if math.isfinite(value) else _INVALID_JSON
    if isinstance(value, list):
        items: list[JsonValue] = []
        for item in cast(list[object], value):
            converted = _json_value(item)
            if isinstance(converted, _InvalidJson):
                return _INVALID_JSON
            items.append(converted.value)
        return _ValidJson(items)
    if isinstance(value, dict):
        fields = _json_object(cast(dict[object, object], value))
        return _INVALID_JSON if fields is None else _ValidJson(fields)
    return _INVALID_JSON
