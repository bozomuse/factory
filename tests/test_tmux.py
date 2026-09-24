"""Behavior tests for tmux-backed Factory state and mailbox operations."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from factory.command import CommandFailure, CommandResult, CommandSuccess
from factory.tmux import (
    InvalidSessionError,
    OperationFailure,
    OperationSuccess,
    TmuxRuntime,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.results: list[CommandResult] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        self.calls.append((tuple(arguments), input_text))
        return self.results.pop(0)


def test_rejects_ambiguous_session_names() -> None:
    runner = FakeRunner()

    with pytest.raises(InvalidSessionError):
        TmuxRuntime.create(runner, "bad:name")


def test_command_exposes_raw_tmux_surface() -> None:
    runner = FakeRunner()
    runner.results = [CommandSuccess("3.6\n")]

    result = TmuxRuntime.create(runner).command("display-message", "-p", "#{version}")

    assert result == OperationSuccess("3.6\n")
    assert runner.calls == [(("tmux", "display-message", "-p", "#{version}"), None)]


def test_command_maps_runner_failure_to_operation_failure() -> None:
    runner = FakeRunner()
    runner.results = [CommandFailure("no server running on /tmp/tmux-1000/default")]

    result = TmuxRuntime.create(runner).command("display-message", "-p", "#{version}")

    assert result == OperationFailure("no server running on /tmp/tmux-1000/default")
    assert runner.calls == [(("tmux", "display-message", "-p", "#{version}"), None)]


def test_ensure_session_preserves_existing_session() -> None:
    runner = FakeRunner()
    runner.results = [CommandSuccess("")]

    result = TmuxRuntime.create(runner).ensure_session()

    assert result == OperationSuccess(None)
    assert len(runner.calls) == 1


def test_ensure_session_recovers_missing_session() -> None:
    runner = FakeRunner()
    runner.results = [CommandFailure("missing"), CommandSuccess("")]

    result = TmuxRuntime.create(runner).ensure_session()

    assert result == OperationSuccess(None)
    assert runner.calls[1][0] == (
        "tmux",
        "new-session",
        "-d",
        "-s",
        "factory",
        "-n",
        "main",
    )


def test_state_reports_windows_and_pane_processes() -> None:
    runner = FakeRunner()
    runner.results = [
        CommandSuccess(
            "factory @1 api 1 %1 0 123 codex 0 1 agent\\ title\n"
            "factory @1 api 1 %2 1 124 bash 1 0 shell\n"
            "other @2 ignored 0 %3 0 125 bash 0 1 other"
        )
    ]

    result = TmuxRuntime.create(runner).state()

    assert isinstance(result, OperationSuccess)
    assert result.value.channel_count == 1
    assert result.value.process_count == 2
    assert result.value.channels[0].channel_id == "@1"
    assert result.value.channels[0].processes[0].command == "codex"
    assert result.value.channels[0].processes[0].title == "agent title"
    assert result.value.channels[0].processes[1].status == "dead"


def test_create_channel_returns_stable_window_id() -> None:
    runner = FakeRunner()
    runner.results = [CommandSuccess("@7\n")]

    result = TmuxRuntime.create(runner).create_channel("worker")

    assert result == OperationSuccess("@7")
    assert runner.calls[0][0][-2:] == ("-n", "worker")


def test_send_message_uses_tmux_paste_buffer() -> None:
    runner = FakeRunner()
    runner.results = [
        CommandSuccess("@7\n"),
        CommandSuccess(""),
        CommandSuccess(""),
        CommandSuccess(""),
    ]

    result = TmuxRuntime.create(runner).send_message("worker", "fix $(nothing)")

    assert result == OperationSuccess(None)
    assert runner.calls[1][0][:2] == ("tmux", "load-buffer")
    assert runner.calls[1][1] == "fix $(nothing)"
    assert runner.calls[2][0][1] == "paste-buffer"
    assert "-p" in runner.calls[2][0]
    assert runner.calls[3][0][-1] == "Enter"


def test_read_channel_captures_requested_scrollback() -> None:
    runner = FakeRunner()
    runner.results = [CommandSuccess("@3\n"), CommandSuccess("output\n")]

    result = TmuxRuntime.create(runner).read_channel("@3", 50)

    assert result == OperationSuccess("output\n")
    assert "-50" in runner.calls[1][0]


def test_invalid_read_limit_does_not_call_tmux() -> None:
    runner = FakeRunner()

    result = TmuxRuntime.create(runner).read_channel("@1", 0)

    assert isinstance(result, OperationFailure)
    assert runner.calls == []
