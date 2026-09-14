"""Tests for the Factory command-line routing."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest


def test_parser_requires_a_command() -> None:
    main_module = importlib.import_module("factory.main")

    with pytest.raises(SystemExit):
        main_module._parser().parse_args([])


def test_state_command_uses_remote_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_module = importlib.import_module("factory.main")
    calls: list[tuple[str, object]] = []

    class Client:
        def __init__(self, config: object) -> None:
            return None

        def call(self, method: str, params: object = None) -> object:
            calls.append((method, params))
            return {"channel_count": 0}

    monkeypatch.setattr(main_module, "FactoryClient", Client)
    args = argparse.Namespace(
        command="state",
        host="localhost",
        port=8443,
        certificate=Path("certificate.pem"),
        server_name=None,
    )

    assert main_module._run(args) == 0
    assert calls == [("work.run", {"unit": "factory.state", "input": {}})]
    assert '"channel_count": 0' in capsys.readouterr().out


def test_send_command_maps_to_mailbox_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("factory.main")
    calls: list[tuple[str, object]] = []

    class Client:
        def __init__(self, config: object) -> None:
            return None

        def call(self, method: str, params: object = None) -> object:
            calls.append((method, params))
            return {"channel": "@1"}

    monkeypatch.setattr(main_module, "FactoryClient", Client)
    args = argparse.Namespace(
        command="send",
        host="localhost",
        port=8443,
        certificate=Path("certificate.pem"),
        server_name=None,
        channel="@1",
        message="fix the tests",
    )

    assert main_module._run(args) == 0
    assert calls == [
        (
            "work.run",
            {
                "unit": "mailbox.send",
                "input": {"channel": "@1", "message": "fix the tests"},
            },
        )
    ]


def test_main_handles_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("factory.main")

    class Parser:
        def parse_args(self) -> argparse.Namespace:
            return argparse.Namespace(command="notifications")

    def interrupt(args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "_parser", Parser)
    monkeypatch.setattr(main_module, "_run", interrupt)

    with pytest.raises(SystemExit, match="130"):
        main_module.main()
