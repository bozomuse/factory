"""Typed subprocess execution shared by operating-system adapters."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandSuccess:
    """The standard output of a successful command."""

    stdout: str


@dataclass(frozen=True, slots=True)
class CommandFailure:
    """A command failure safe to expose as an operational error."""

    message: str


type CommandResult = CommandSuccess | CommandFailure


class CommandRunner(Protocol):
    """The subprocess behavior required by Factory adapters."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        """Run a command and capture its result."""
        ...


class SubprocessRunner:
    """Run commands without a shell or command-string interpolation."""

    def __init__(self, timeout: float | None = None) -> None:
        self._timeout = timeout

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        """Run a command and return a typed result."""
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                input=input_text,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return CommandFailure("command timed out")
        except OSError as error:
            return CommandFailure(str(error))
        if completed.returncode == 0:
            return CommandSuccess(completed.stdout)
        message = completed.stderr.strip() or completed.stdout.strip()
        return CommandFailure(message or f"command exited with {completed.returncode}")
