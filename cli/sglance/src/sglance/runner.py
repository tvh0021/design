from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sglance.models import ClusterEntry

_ALLOWED_BINARIES = frozenset({"sinfo", "squeue", "sacct", "scontrol"})


class AllowlistError(ValueError):
    """Raised when a command vector violates the read-only Slurm allowlist."""


class TransportError(Exception):
    """Raised when reaching or executing on a cluster fails at the transport layer."""


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
