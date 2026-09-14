"""Dynamic host setup, TLS identity generation, and reboot-safe startup."""

from __future__ import annotations

import getpass
import os
import platform
import pwd
import shlex
import shutil
import socket
import ssl
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from factory.command import CommandFailure, CommandRunner


class PackageManager(StrEnum):
    """Supported host package managers."""

    APT = "apt-get"
    DNF = "dnf"
    YUM = "yum"
    APK = "apk"
    PACMAN = "pacman"
    ZYPPER = "zypper"
    BREW = "brew"


_PACKAGE_MANAGERS: tuple[PackageManager, ...] = (
    PackageManager.APT,
    PackageManager.DNF,
    PackageManager.YUM,
    PackageManager.APK,
    PackageManager.PACMAN,
    PackageManager.ZYPPER,
    PackageManager.BREW,
)


@dataclass(frozen=True, slots=True)
class SetupFailure:
    """An actionable setup failure."""

    message: str


@dataclass(frozen=True, slots=True)
class SetupSuccess:
    """Paths a client needs after successful setup."""

    certificate: Path
    service: str


type SetupResult = SetupSuccess | SetupFailure


@dataclass(frozen=True, slots=True)
class SetupConfig:
    """Settings persisted in the generated Factory service."""

    host: str
    port: int
    session: str
    server_name: str


@dataclass(frozen=True, slots=True)
class SetupEnvironment:
    """Detected host details needed to install Factory."""

    system: str
    manager: PackageManager | None
    user: str
    home: Path
    executable: Path
    working_directory: Path
    elevate: tuple[str, ...]
    path: str

    @classmethod
    def detect(cls) -> SetupEnvironment | SetupFailure:
        """Detect the operating system, package manager, and target user."""
        system = platform.system()
        manager = next(
            (
                item
                for item in _PACKAGE_MANAGERS
                if shutil.which(item.value) is not None
            ),
            None,
        )
        user = os.environ.get("SUDO_USER") or getpass.getuser()
        try:
            home = Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            return SetupFailure(f"could not determine home directory for {user}")
        if os.geteuid() == 0:
            elevate: tuple[str, ...] = ()
        elif shutil.which("sudo") is not None:
            elevate = ("sudo",)
        else:
            return SetupFailure("setup requires root or sudo")
        return cls(
            system=system,
            manager=manager,
            user=user,
            home=home,
            executable=Path(sys.executable).resolve(),
            working_directory=Path.cwd().resolve(),
            elevate=elevate,
            path=f"{home}/.local/bin:{os.environ.get('PATH', '')}",
        )


class FactorySetup:
    """Install host tools and configure Factory to start after reboots."""

    def __init__(self, runner: CommandRunner, environment: SetupEnvironment) -> None:
        self._runner = runner
        self._environment = environment

    def run(self, config: SetupConfig) -> SetupResult:
        """Install dependencies, create TLS keys, and enable autostart."""
        error = _validate_config(config)
        if error is not None:
            return SetupFailure(error)
        install_error = self._install_tools()
        if install_error is not None:
            return install_error

        data_directory = self._environment.home / ".local/share/factory"
        certificate = data_directory / "certificate.pem"
        private_key = data_directory / "private-key.pem"
        try:
            _ensure_certificate(certificate, private_key, config.server_name)
        except OSError as error:
            return SetupFailure(f"could not create TLS certificate: {error}")

        if self._environment.system == "Linux":
            service = self._install_systemd_service(
                config,
                certificate,
                private_key,
                data_directory / "notifications.jsonl",
            )
        elif self._environment.system == "Darwin":
            service = self._install_launchd_service(
                config,
                certificate,
                private_key,
                data_directory / "notifications.jsonl",
            )
        else:
            return SetupFailure(
                f"automatic startup is not supported on {self._environment.system}"
            )
        if isinstance(service, SetupFailure):
            return service
        return SetupSuccess(certificate, service.service)

    def _install_tools(self) -> SetupFailure | None:
        missing = [
            program
            for program in ("tmux", "git", "curl", "cc", "make", "node", "npm")
            if shutil.which(program) is None
        ]
        if missing:
            manager = self._environment.manager
            if manager is None:
                return SetupFailure(
                    "no supported package manager found; missing " + ", ".join(missing)
                )
            for command in _package_commands(manager, self._environment.elevate):
                result = self._runner.run(command)
                if isinstance(result, CommandFailure):
                    return SetupFailure(result.message)
        if shutil.which("codex") is None:
            result = self._runner.run(
                (*self._environment.elevate, "npm", "install", "-g", "@openai/codex")
            )
            if isinstance(result, CommandFailure):
                return SetupFailure(result.message)
        version_arguments = {
            "tmux": ("tmux", "-V"),
            "git": ("git", "--version"),
            "curl": ("curl", "--version"),
            "cc": ("cc", "--version"),
            "make": ("make", "--version"),
            "node": ("node", "--version"),
            "npm": ("npm", "--version"),
            "codex": ("codex", "--version"),
        }
        for program, arguments in version_arguments.items():
            checked = self._runner.run(arguments)
            if isinstance(checked, CommandFailure):
                return SetupFailure(f"{program} is unavailable after installation")
        return None

    def _install_systemd_service(
        self,
        config: SetupConfig,
        certificate: Path,
        private_key: Path,
        notifications: Path,
    ) -> SetupResult:
        if shutil.which("systemctl") is None:
            return SetupFailure("systemd is required for reboot-safe startup")
        unit = _systemd_unit(
            self._environment,
            config,
            certificate,
            private_key,
            notifications,
        )
        temporary = _temporary_file(unit)
        try:
            installed = self._runner.run(
                (
                    *self._environment.elevate,
                    "install",
                    "-m",
                    "0644",
                    str(temporary),
                    "/etc/systemd/system/factory.service",
                )
            )
        finally:
            temporary.unlink(missing_ok=True)
        if isinstance(installed, CommandFailure):
            return SetupFailure(installed.message)
        for arguments in (
            (*self._environment.elevate, "systemctl", "daemon-reload"),
            (
                *self._environment.elevate,
                "systemctl",
                "enable",
                "factory.service",
            ),
            (*self._environment.elevate, "systemctl", "restart", "factory.service"),
        ):
            result = self._runner.run(arguments)
            if isinstance(result, CommandFailure):
                return SetupFailure(result.message)
        active = self._runner.run(
            (
                *self._environment.elevate,
                "systemctl",
                "is-active",
                "--quiet",
                "factory.service",
            )
        )
        if isinstance(active, CommandFailure):
            return SetupFailure("factory.service did not start successfully")
        return SetupSuccess(certificate, "factory.service")

    def _install_launchd_service(
        self,
        config: SetupConfig,
        certificate: Path,
        private_key: Path,
        notifications: Path,
    ) -> SetupResult:
        launch_agents = self._environment.home / "Library/LaunchAgents"
        launch_agents.mkdir(parents=True, exist_ok=True)
        service_path = launch_agents / "dev.factory.plist"
        service_path.write_text(
            _launchd_plist(
                self._environment,
                config,
                certificate,
                private_key,
                notifications,
            ),
            encoding="utf-8",
        )
        self._runner.run(
            (
                "launchctl",
                "bootout",
                f"gui/{os.getuid()}/dev.factory",
            )
        )
        result = self._runner.run(
            (
                "launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                str(service_path),
            )
        )
        if isinstance(result, CommandFailure):
            return SetupFailure(result.message)
        return SetupSuccess(certificate, "dev.factory")


def _validate_config(config: SetupConfig) -> str | None:
    if not config.host:
        return "host must not be empty"
    if not 1 <= config.port <= 65_535:
        return "port must be between 1 and 65535"
    if not config.session or ":" in config.session or "." in config.session:
        return "session must be a non-empty tmux session name"
    if not config.server_name:
        return "server_name must not be empty"
    return None


def _package_commands(
    manager: PackageManager,
    elevate: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    packages = {
        PackageManager.APT: ("tmux", "git", "curl", "build-essential", "nodejs", "npm"),
        PackageManager.DNF: (
            "tmux",
            "git",
            "curl",
            "gcc",
            "gcc-c++",
            "make",
            "nodejs",
            "npm",
        ),
        PackageManager.YUM: (
            "tmux",
            "git",
            "curl",
            "gcc",
            "gcc-c++",
            "make",
            "nodejs",
            "npm",
        ),
        PackageManager.APK: ("tmux", "git", "curl", "build-base", "nodejs", "npm"),
        PackageManager.PACMAN: ("tmux", "git", "curl", "base-devel", "nodejs", "npm"),
        PackageManager.ZYPPER: (
            "tmux",
            "git",
            "curl",
            "-t",
            "pattern",
            "devel_basis",
            "nodejs",
            "npm",
        ),
        PackageManager.BREW: ("tmux", "git", "curl", "node"),
    }[manager]
    if manager is PackageManager.APT:
        return (
            (*elevate, manager.value, "update"),
            (*elevate, manager.value, "install", "-y", *packages),
        )
    if manager is PackageManager.PACMAN:
        return ((*elevate, manager.value, "-Sy", "--needed", "--noconfirm", *packages),)
    if manager is PackageManager.APK:
        return ((*elevate, manager.value, "add", *packages),)
    if manager is PackageManager.BREW:
        return ((manager.value, "install", *packages),)
    return ((*elevate, manager.value, "install", "-y", *packages),)


def _ensure_certificate(
    certificate: Path,
    private_key_path: Path,
    server_name: str,
) -> None:
    if _certificate_is_valid(certificate, private_key_path):
        return
    certificate.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_name)])
    names: list[x509.GeneralName] = [
        x509.DNSName(server_name),
        x509.DNSName("localhost"),
        x509.DNSName(socket.gethostname()),
        x509.IPAddress(ip_address("127.0.0.1")),
    ]
    try:
        address = ip_address(server_name)
    except ValueError:
        pass
    else:
        if not address.is_unspecified:
            names.append(x509.IPAddress(address))
    now = datetime.now(UTC)
    signed = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    private_key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    certificate_bytes = signed.public_bytes(serialization.Encoding.PEM)
    _write_atomic(private_key_path, private_key_bytes, mode=0o600)
    _write_atomic(certificate, certificate_bytes, mode=0o644)


def _certificate_is_valid(certificate: Path, private_key: Path) -> bool:
    if not certificate.exists() or not private_key.exists():
        return False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certificate, private_key)
    except (OSError, ssl.SSLError):
        return False
    return True


def _write_atomic(path: Path, content: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _server_arguments(
    environment: SetupEnvironment,
    config: SetupConfig,
    certificate: Path,
    private_key: Path,
    notifications: Path,
) -> tuple[str, ...]:
    return (
        str(environment.executable),
        "-m",
        "factory",
        "start",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--session",
        config.session,
        "--certificate",
        str(certificate),
        "--private-key",
        str(private_key),
        "--notifications",
        str(notifications),
    )


def _systemd_unit(
    environment: SetupEnvironment,
    config: SetupConfig,
    certificate: Path,
    private_key: Path,
    notifications: Path,
) -> str:
    command = shlex.join(
        _server_arguments(environment, config, certificate, private_key, notifications)
    )
    return f"""[Unit]
Description=Factory agent runtime
After=network.target

[Service]
Type=simple
User={environment.user}
WorkingDirectory={environment.working_directory}
Environment=HOME={environment.home}
Environment=PATH={environment.path}
ExecStart={command}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def _launchd_plist(
    environment: SetupEnvironment,
    config: SetupConfig,
    certificate: Path,
    private_key: Path,
    notifications: Path,
) -> str:
    arguments = "\n".join(
        f"        <string>{_xml_escape(argument)}</string>"
        for argument in _server_arguments(
            environment, config, certificate, private_key, notifications
        )
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>dev.factory</string>
    <key>ProgramArguments</key>
    <array>
{arguments}
    </array>
    <key>WorkingDirectory</key><string>{_xml_escape(str(environment.working_directory))}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>{_xml_escape(str(environment.home))}</string>
        <key>PATH</key><string>{_xml_escape(environment.path)}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
"""


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _temporary_file(content: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="factory-", suffix=".service")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    return Path(name)
