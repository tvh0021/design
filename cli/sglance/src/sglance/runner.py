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
_SSH_BASE_ARGS = (
    "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
    "-o", "RemoteCommand=none", "-o", "LogLevel=ERROR", "--",
)


class AllowlistError(ValueError):
    """Raised when a command vector violates the read-only Slurm allowlist."""


class TransportError(Exception):
    """Raised when reaching or executing on a cluster fails at the transport layer."""


def last_line(text: str, default: str = "") -> str:
    return next((s for line in reversed(text.splitlines()) if (s := line.strip())), default)


def format_transport_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if not msg:
        return "unreachable"
    return msg if msg.startswith("unreachable") else f"unreachable ({msg})"


def format_ssh_exit_255(stderr: str) -> str:
    return f"unreachable (ssh: {last_line(stderr, 'exit 255').removeprefix('ssh: ')})"


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
    rest = list(argv[3:])
    for idx, token in enumerate(rest):
        if token == "-M" and (idx + 1 >= len(rest) or rest[idx + 1].startswith("-")):
            raise AllowlistError("Flag '-M' requires a cluster name not starting with '-'.")
    if binary == "scontrol":
        if len(rest) >= 2 and rest[0] == "-M":
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


class TransportDispatcher:
    """Executes allowlisted Slurm commands locally or over OpenSSH."""

    def run(self, cluster_entry: ClusterEntry, argv: Sequence[str]) -> CommandResult:
        if cluster_entry.local:
            cmd, kind = list(argv), "command"
        else:
            if not cluster_entry.ssh or cluster_entry.ssh.startswith("-"):
                raise TransportError("unreachable (invalid ssh destination)")
            cmd, kind = [*_SSH_BASE_ARGS, cluster_entry.ssh, shlex.join(argv)], "ssh"
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLUSTER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportError("unreachable (timed out after 25s)") from exc
        except OSError as exc:
            raise TransportError(f"unreachable (could not run {kind}: {exc.strerror or exc})") from exc
        if proc.returncode < 0:
            raise TransportError(f"unreachable ({kind} killed by signal {-proc.returncode})")
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
