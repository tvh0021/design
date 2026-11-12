from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import shlex
import subprocess
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sglance.models import ClusterEntry

_ALLOWED_BINARIES = frozenset({"sinfo", "squeue", "sacct", "scontrol"})
CLUSTER_TIMEOUT_SECONDS = 25.0
_SSH_BASE_ARGS = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--")


class AllowlistError(ValueError):
    """Raised when a command vector violates the read-only Slurm allowlist."""


class TransportError(Exception):
    """Raised when reaching or executing on a cluster fails at the transport layer."""


def format_transport_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if not msg:
        return "unreachable"
    if msg.startswith("unreachable"):
        return msg
    return f"unreachable ({msg})"


def format_ssh_exit_255(stderr: str) -> str:
    last_line = next(
        (line.strip() for line in reversed(stderr.splitlines()) if line.strip()),
        "exit 255",
    )
    if last_line.startswith("ssh: "):
        last_line = last_line[len("ssh: ") :]
    return f"unreachable (ssh: {last_line})"


def _format_timeout_seconds(seconds: float) -> str:
    return str(int(seconds)) if float(seconds).is_integer() else str(seconds)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


class CommandRunner(Protocol):
    def run(self, cluster_entry: ClusterEntry, argv: Sequence[str]) -> CommandResult: ...


def validate_allowlist(argv: Sequence[str]) -> None:
    if len(argv) < 3 or any(not isinstance(token, str) or not token for token in argv):
        raise AllowlistError("Command vector must be a non-empty string sequence with env prefix.")
    if argv[0] != "env" or argv[1] != "SLURM_TIME_FORMAT=%s":
        raise AllowlistError("Command vector must start with ['env', 'SLURM_TIME_FORMAT=%s'].")
    binary = argv[2]
    if binary not in _ALLOWED_BINARIES:
        raise AllowlistError(f"Command binary {binary!r} is not on the read-only allowlist.")
    if binary == "scontrol":
        rest = list(argv[3:])
        if len(rest) >= 2 and rest[0] == "-M" and not rest[1].startswith("-"):
            rest = rest[2:]
        if rest != ["show", "config"]:
            raise AllowlistError("Only 'scontrol show config' is permitted.")


class AllowlistedRunner:
    """Single enforcement point for the fixed read-only Slurm command allowlist."""

    def __init__(self, inner: CommandRunner) -> None:
        self._inner = inner

    def run(self, cluster_entry: ClusterEntry, argv: Sequence[str]) -> CommandResult:
        validate_allowlist(argv)
        return self._inner.run(cluster_entry, argv)


class SshRunner:
    """Executes allowlisted Slurm commands on a remote cluster entry over OpenSSH."""

    def __init__(self, *, timeout_seconds: float = CLUSTER_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def run(self, cluster_entry: ClusterEntry, argv: Sequence[str]) -> CommandResult:
        if not cluster_entry.ssh or cluster_entry.ssh.startswith("-"):
            raise TransportError("unreachable (invalid ssh destination)")
        remote_command = shlex.join(argv)
        ssh_argv = [*_SSH_BASE_ARGS, cluster_entry.ssh, remote_command]
        try:
            proc = subprocess.run(
                ssh_argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            label = _format_timeout_seconds(self._timeout_seconds)
            raise TransportError(f"unreachable (timed out after {label}s)") from exc
        except OSError as exc:
            raise TransportError(
                f"unreachable (could not run ssh: {exc.strerror or exc})"
            ) from exc

        if proc.returncode == 255:
            raise TransportError(format_ssh_exit_255(proc.stderr))
        if proc.returncode < 0:
            raise TransportError(f"unreachable (ssh killed by signal {-proc.returncode})")
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class LocalRunner:
    """Executes allowlisted Slurm commands directly on a local login node."""

    def __init__(self, *, timeout_seconds: float = CLUSTER_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def run(self, cluster_entry: ClusterEntry, argv: Sequence[str]) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            label = _format_timeout_seconds(self._timeout_seconds)
            raise TransportError(f"unreachable (timed out after {label}s)") from exc
        except OSError as exc:
            raise TransportError(
                f"unreachable (could not run command: {exc.strerror or exc})"
            ) from exc

        if proc.returncode < 0:
            raise TransportError(f"unreachable (command killed by signal {-proc.returncode})")
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class TransportDispatcher:
    """Dispatches cluster entries to LocalRunner (local=True) or SshRunner (local=False)."""

    def __init__(self, *, timeout_seconds: float = CLUSTER_TIMEOUT_SECONDS) -> None:
        self._ssh = SshRunner(timeout_seconds=timeout_seconds)
        self._local = LocalRunner(timeout_seconds=timeout_seconds)

    def run(self, cluster_entry: ClusterEntry, argv: Sequence[str]) -> CommandResult:
        if cluster_entry.local:
            return self._local.run(cluster_entry, argv)
        return self._ssh.run(cluster_entry, argv)

