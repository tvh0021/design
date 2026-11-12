from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import mkdtemp
import threading
from typing import Protocol

from sglance import CommandResult


def make_test_env(
    root: Path,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an isolated XDG/HOME env under *root* with a fresh XDG_CACHE_HOME."""
    root.mkdir(parents=True, exist_ok=True)
    env = {
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": mkdtemp(prefix="cache-", dir=root),
        "HOME": str(root / "home"),
    }
    if extra is not None:
        env.update(extra)
    return env


class _ClusterTarget(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def ssh(self) -> str | None: ...

    @property
    def local(self) -> bool: ...


RunnerResponse = str | CommandResult | Exception
LookupKey = tuple[str, ...]
RecordedCall = tuple[str, str, str | None, list[str]]


class UnmatchedFakeRequest(BaseException):
    """Raised when FakeRunner receives a command with no configured response."""


class FakeRunner:
    """In-memory command runner test double for CLI seam tests."""

    def __init__(self, responses: Mapping[LookupKey, RunnerResponse] | None = None) -> None:
        validated: dict[LookupKey, RunnerResponse] = {}
        for key, value in (responses or {}).items():
            if (
                not isinstance(key, tuple)
                or len(key) < 2
                or not all(isinstance(part, str) and part for part in key)
            ):
                raise ValueError(
                    f"FakeRunner response key must be (cluster_name, binary, ...), got {key!r}"
                )
            validated[key] = value
        self._responses = validated
        self._lock = threading.Lock()
        self.calls: list[RecordedCall] = []

    def run(self, cluster_entry: _ClusterTarget, argv: Sequence[str]) -> CommandResult:
        cluster_name = cluster_entry.name
        transport_kind = "local" if cluster_entry.local else "ssh"
        target = None if cluster_entry.local else cluster_entry.ssh
        argv_list = list(argv)

        with self._lock:
            self.calls.append((cluster_name, transport_kind, target, argv_list))

        binary = (
            argv_list[2]
            if len(argv_list) >= 3 and argv_list[0] == "env"
            else (argv_list[0] if argv_list else "")
        )
        candidates: tuple[LookupKey, ...] = (
            (cluster_name, *argv_list),
            (cluster_name, binary),
        )
        for key in candidates:
            if key in self._responses:
                outcome = self._responses[key]
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, CommandResult):
                    return outcome
                return CommandResult(returncode=0, stdout=outcome, stderr="")
        raise UnmatchedFakeRequest(
            f"Unmatched FakeRunner request for cluster={cluster_name!r}, "
            f"binary={binary!r}, argv={argv_list!r}; "
            f"configured keys={list(self._responses)!r}"
        )
