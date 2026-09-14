"""Authoritative state and mailbox operations backed by tmux."""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from typing import Literal

from factory.command import CommandFailure, CommandRunner, CommandSuccess

_PANE_FORMAT = " ".join(
    (
        "#{q:session_name}",
        "#{q:window_id}",
        "#{q:window_name}",
        "#{window_active}",
        "#{q:pane_id}",
        "#{pane_index}",
        "#{pane_pid}",
        "#{q:pane_current_command}",
        "#{pane_dead}",
        "#{pane_active}",
        "#{q:pane_title}",
    )
)


class TmuxError(Exception):
    """Base class for tmux runtime configuration errors."""


class InvalidSessionError(TmuxError):
    """Raised when a tmux session name is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class OperationSuccess[T]:
    """A successful tmux operation."""

    value: T


@dataclass(frozen=True, slots=True)
class OperationFailure:
    """An expected tmux operation failure."""

    message: str


type OperationResult[T] = OperationSuccess[T] | OperationFailure
type ProcessStatus = Literal["running", "dead"]


@dataclass(frozen=True, slots=True)
class ProcessState:
    """The live state of one tmux pane process."""

    pane_id: str
    pane_index: int
    pid: int
    command: str
    status: ProcessStatus
    active: bool
    title: str


@dataclass(frozen=True, slots=True)
class ChannelState:
    """The live state of one channel, represented by a tmux window."""

    channel_id: str
    name: str
    active: bool
    processes: tuple[ProcessState, ...]


@dataclass(frozen=True, slots=True)
class FactoryState:
    """A current snapshot of the Factory tmux session."""

    session: str
    channels: tuple[ChannelState, ...]

    @property
    def channel_count(self) -> int:
        """Return the number of current tmux windows."""
        return len(self.channels)

    @property
    def process_count(self) -> int:
        """Return the number of current tmux panes."""
        return sum(len(channel.processes) for channel in self.channels)


class TmuxRuntime:
    """Control one tmux session and inspect it afresh for every read."""

    def __init__(self, runner: CommandRunner, session: str) -> None:
        self._runner = runner
        self._session = session

    @classmethod
    def create(cls, runner: CommandRunner, session: str = "factory") -> TmuxRuntime:
        """Create a runtime for a validated tmux session name."""
        if not session or ":" in session or "." in session:
            raise InvalidSessionError("session must be a non-empty tmux session name")
        return cls(runner, session)

    def ensure_session(self) -> OperationResult[None]:
        """Create the tmux session when it does not already exist."""
        exists = self._runner.run(("tmux", "has-session", "-t", self._session))
        if isinstance(exists, CommandSuccess):
            return OperationSuccess(None)
        created = self._runner.run(
            ("tmux", "new-session", "-d", "-s", self._session, "-n", "main")
        )
        return _without_output(created)

    def state(self) -> OperationResult[FactoryState]:
        """Read current windows, panes, processes, and statuses from tmux."""
        result = self._runner.run(("tmux", "list-panes", "-a", "-F", _PANE_FORMAT))
        if isinstance(result, CommandFailure):
            return OperationFailure(result.message)
        return _parse_state(self._session, result.stdout)

    def create_channel(self, name: str) -> OperationResult[str]:
        """Create a tmux window and return its stable window id."""
        if not name or "\n" in name:
            return OperationFailure("channel name must be non-empty and single-line")
        result = self._runner.run(
            (
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}",
                "-t",
                self._session,
                "-n",
                name,
            )
        )
        if isinstance(result, CommandFailure):
            return OperationFailure(result.message)
        channel_id = result.stdout.strip()
        if not channel_id.startswith("@"):
            return OperationFailure("tmux returned an invalid channel id")
        return OperationSuccess(channel_id)

    def send_message(self, channel: str, message: str) -> OperationResult[None]:
        """Paste a message into the channel's active pane and press Enter."""
        resolved = self._resolve_channel(channel)
        if isinstance(resolved, OperationFailure):
            return resolved

        target = f"{self._session}:{resolved.value}"
        buffer_name = f"factory-{uuid.uuid4().hex}"
        loaded = self._runner.run(
            ("tmux", "load-buffer", "-b", buffer_name, "-"),
            input_text=message,
        )
        if isinstance(loaded, CommandFailure):
            return OperationFailure(loaded.message)
        pasted = self._runner.run(
            (
                "tmux",
                "paste-buffer",
                "-p",
                "-d",
                "-b",
                buffer_name,
                "-t",
                target,
            )
        )
        if isinstance(pasted, CommandFailure):
            self._runner.run(("tmux", "delete-buffer", "-b", buffer_name))
            return OperationFailure(pasted.message)
        entered = self._runner.run(("tmux", "send-keys", "-t", target, "Enter"))
        return _without_output(entered)

    def read_channel(self, channel: str, lines: int = 200) -> OperationResult[str]:
        """Capture recent visible and scrollback text from a channel."""
        if not 1 <= lines <= 10_000:
            return OperationFailure("lines must be between 1 and 10000")
        resolved = self._resolve_channel(channel)
        if isinstance(resolved, OperationFailure):
            return resolved
        result = self._runner.run(
            (
                "tmux",
                "capture-pane",
                "-p",
                "-S",
                f"-{lines}",
                "-t",
                f"{self._session}:{resolved.value}",
            )
        )
        if isinstance(result, CommandFailure):
            return OperationFailure(result.message)
        return OperationSuccess(result.stdout)

    def _resolve_channel(self, channel: str) -> OperationResult[str]:
        if not channel or "\n" in channel:
            return OperationFailure("channel must be a tmux window id or name")
        result = self._runner.run(
            (
                "tmux",
                "display-message",
                "-p",
                "-t",
                f"{self._session}:{channel}",
                "#{window_id}",
            )
        )
        if isinstance(result, CommandFailure):
            return OperationFailure(result.message)
        channel_id = result.stdout.strip()
        if not channel_id.startswith("@"):
            return OperationFailure("tmux returned an invalid channel id")
        return OperationSuccess(channel_id)


def _without_output(result: CommandSuccess | CommandFailure) -> OperationResult[None]:
    if isinstance(result, CommandFailure):
        return OperationFailure(result.message)
    return OperationSuccess(None)


def _parse_state(session: str, output: str) -> OperationResult[FactoryState]:
    channels: dict[str, tuple[str, bool, list[ProcessState]]] = {}
    for line in output.splitlines():
        if not line:
            continue
        try:
            fields = shlex.split(line)
        except ValueError:
            return OperationFailure("tmux returned an invalid state record")
        if len(fields) != 11:
            return OperationFailure("tmux returned an invalid state record")
        (
            record_session,
            channel_id,
            channel_name,
            channel_active,
            pane_id,
            pane_index,
            pane_pid,
            command,
            pane_dead,
            pane_active,
            title,
        ) = fields
        if record_session != session:
            continue
        try:
            process = ProcessState(
                pane_id=pane_id,
                pane_index=int(pane_index),
                pid=int(pane_pid),
                command=command,
                status="dead" if pane_dead == "1" else "running",
                active=pane_active == "1",
                title=title,
            )
        except ValueError:
            return OperationFailure("tmux returned non-numeric pane state")
        existing = channels.get(channel_id)
        if existing is None:
            channels[channel_id] = (channel_name, channel_active == "1", [process])
        else:
            existing[2].append(process)

    state = FactoryState(
        session=session,
        channels=tuple(
            ChannelState(channel_id, name, active, tuple(processes))
            for channel_id, (name, active, processes) in channels.items()
        ),
    )
    return OperationSuccess(state)
