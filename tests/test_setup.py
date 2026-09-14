"""Behavior tests for dynamic Factory host setup."""

from __future__ import annotations

import getpass
import os
import platform
import pwd
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from factory.command import CommandResult, CommandSuccess
from factory.setup import (
    FactorySetup,
    PackageManager,
    SetupConfig,
    SetupEnvironment,
    SetupFailure,
    SetupSuccess,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(arguments))
        return CommandSuccess("")


class TemporaryPath:
    def __init__(self) -> None:
        self.unlinked = False

    def __str__(self) -> str:
        return "/tmp/factory.service"

    def unlink(self, *, missing_ok: bool) -> None:
        self.unlinked = True


def _environment() -> SetupEnvironment:
    return SetupEnvironment(
        system="Linux",
        manager=PackageManager.APT,
        user="developer",
        home=Path("/home/developer"),
        executable=Path("/opt/factory/bin/python"),
        working_directory=Path("/work/factory"),
        elevate=("sudo",),
        path="/home/developer/.local/bin:/usr/local/bin:/usr/bin:/bin",
    )


def test_detect_environment_selects_available_package_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def find_program(program: str) -> str | None:
        return "/usr/bin/apt-get" if program in {"apt-get", "sudo"} else None

    def user_record(user: str) -> SimpleNamespace:
        assert user == "developer"
        return SimpleNamespace(pw_dir="/home/developer")

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", find_program)
    monkeypatch.setattr(getpass, "getuser", lambda: "developer")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(pwd, "getpwnam", user_record)

    environment = SetupEnvironment.detect()

    assert isinstance(environment, SetupEnvironment)
    assert environment.manager is PackageManager.APT
    assert environment.elevate == ("sudo",)


def test_setup_installs_tools_certificate_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import factory.setup as setup_module

    runner = RecordingRunner()
    temporary = TemporaryPath()
    generated: list[tuple[Path, Path, str]] = []
    units: list[str] = []

    def find_program(program: str) -> str | None:
        return "/usr/bin/systemctl" if program == "systemctl" else None

    def ensure_certificate(certificate: Path, key: Path, name: str) -> None:
        generated.append((certificate, key, name))

    def temporary_file(content: str) -> Path:
        units.append(content)
        return cast(Path, temporary)

    monkeypatch.setattr(shutil, "which", find_program)
    monkeypatch.setattr(setup_module, "_ensure_certificate", ensure_certificate)
    monkeypatch.setattr(setup_module, "_temporary_file", temporary_file)

    result = FactorySetup(runner, _environment()).run(
        SetupConfig("127.0.0.1", 8443, "factory", "factory.test")
    )

    assert result == SetupSuccess(
        Path("/home/developer/.local/share/factory/certificate.pem"),
        "factory.service",
    )
    assert generated[0][2] == "factory.test"
    assert ("sudo", "apt-get", "update") in runner.calls
    assert (
        "sudo",
        "npm",
        "install",
        "-g",
        "@openai/codex",
    ) in runner.calls
    assert runner.calls[-2][-2:] == ("restart", "factory.service")
    assert runner.calls[-1][-3:] == ("is-active", "--quiet", "factory.service")
    assert "User=developer" in units[0]
    assert "Environment=PATH=/home/developer/.local/bin" in units[0]
    assert "Restart=always" in units[0]
    assert "factory start --host 127.0.0.1" in units[0]
    assert temporary.unlinked


def test_setup_rejects_invalid_config_without_side_effects() -> None:
    runner = RecordingRunner()

    result = FactorySetup(runner, _environment()).run(
        SetupConfig("", 8443, "factory", "factory.test")
    )

    assert isinstance(result, SetupFailure)
    assert runner.calls == []
