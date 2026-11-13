from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import io
from pathlib import Path
from tempfile import mkdtemp
import threading
from typing import Protocol

from sglance import CommandResult, main

# 2026-11-12T20:00:00Z == 1794513600 Unix epoch seconds.
FIXED_NOW = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)
DEFAULT_CLUSTER_TOML = '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n'


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


def write_config(root: Path, content: str = DEFAULT_CLUSTER_TOML) -> Path:
    """Write sglance config.toml under root/config/sglance/."""
    config_dir = root / "config" / "sglance"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def run_cli(
    argv: Sequence[str],
    *,
    root: Path,
    runner: FakeRunner | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime = FIXED_NOW,
) -> tuple[int, str, str]:
    """Invoke main() with isolated env, FakeRunner, and fixed clock."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    effective_env = make_test_env(root) if env is None else env
    rc = main(
        argv,
        env=effective_env,
        runner=runner,
        clock=lambda: now,
        stdout=stdout,
        stderr=stderr,
    )
    return rc, stdout.getvalue(), stderr.getvalue()


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
        self._responses: dict[LookupKey, RunnerResponse] = {}
        for key, value in (responses or {}).items():
            self.set_response(key, value)
        self._lock = threading.Lock()
        self.calls: list[RecordedCall] = []

    def set_response(self, key: LookupKey, value: RunnerResponse) -> None:
        if (
            not isinstance(key, tuple)
            or len(key) < 2
            or not all(isinstance(part, str) and part for part in key)
        ):
            raise ValueError(
                f"FakeRunner response key must be (cluster_name, binary, ...), got {key!r}"
            )
        self._responses[key] = value

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
        for key in ((cluster_name, *argv_list), (cluster_name, binary)):
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
