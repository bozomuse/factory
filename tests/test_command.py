"""Tests for typed subprocess execution."""

from __future__ import annotations

import subprocess

import pytest

from factory.command import CommandFailure, CommandSuccess, SubprocessRunner


def test_subprocess_runner_returns_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["tool"], 0, stdout="done\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    result = SubprocessRunner().run(("tool",), input_text="input")

    assert result == CommandSuccess("done\n")


def test_subprocess_runner_returns_command_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["tool"], 2, stdout="", stderr="bad\n")

    monkeypatch.setattr(subprocess, "run", run)

    assert SubprocessRunner().run(("tool",)) == CommandFailure("bad")


def test_subprocess_runner_returns_spawn_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tool")

    monkeypatch.setattr(subprocess, "run", run)

    assert SubprocessRunner().run(("tool",)) == CommandFailure("tool")


def test_subprocess_runner_limits_runtime_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["tool"], 10)

    monkeypatch.setattr(subprocess, "run", run)

    assert SubprocessRunner(timeout=10).run(("tool",)) == CommandFailure(
        "command timed out"
    )
