from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
import sys
from typing import NoReturn, TextIO

from sglance.cache import cached_cluster_fetch
from sglance.config import ConfigError, load_config
from sglance.formatters import format_status_json, format_status_table
from sglance.models import ClusterEntry, ClusterStatus, StatusReport
from sglance.runner import (
    AllowlistError,
    AllowlistedRunner,
    CommandRunner,
    TransportDispatcher,
)
from sglance.slurm import SlurmAdapter


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _build_parser(stdout: TextIO, stderr: TextIO) -> argparse.ArgumentParser:
    class _CliParser(argparse.ArgumentParser):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.color = False

        def _print_message(self, message: str, file: TextIO | None = None) -> None:
            if not message:
                return
            target = stdout if file is sys.stdout else stderr
            target.write(message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            if message:
                self._print_message(message, sys.stderr)
            raise _ParserExit(status)

    parser = _CliParser(
        prog="sglance",
        description="Read-only inspection of Slurm cluster capacity, queues, and wait times.",
    )
    parser.add_argument(
        "--cluster",
        dest="cluster",
        default=None,
        metavar="NAME",
        help="Query only the named configured cluster entry.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=False,
        help="Bypass the 5-minute response cache and fetch fresh Slurm data.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True, parser_class=_CliParser)
    status_parser = subparsers.add_parser("status", help="Show partition CPU, GPU, and node utilization.")
    status_parser.add_argument(
        "--cluster",
        dest="cluster",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help="Query only the named configured cluster entry.",
    )
    status_parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=argparse.SUPPRESS,
        help="Bypass the 5-minute response cache and fetch fresh Slurm data.",
    )
    status_parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON output.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    clock: Callable[[], datetime] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    effective_env = os.environ if env is None else env
    effective_clock = (lambda: datetime.now(UTC)) if clock is None else clock
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    parser = _build_parser(out, err)
    try:
        parsed = parser.parse_args(args_list)
    except _ParserExit as exc:
        return exc.status

    try:
        cluster_entries = load_config(effective_env)
    except ConfigError as exc:
        err.write(f"sglance: error: {exc}\n")
        return 2

    if parsed.cluster is not None:
        selected_entries = tuple(
            entry for entry in cluster_entries if entry.name == parsed.cluster
        )
        if not selected_entries:
            configured = ", ".join(entry.name for entry in cluster_entries)
            err.write(
                f"sglance: error: unknown cluster {parsed.cluster!r} (configured: {configured})\n"
            )
            return 2
    else:
        selected_entries = cluster_entries

    effective_runner: CommandRunner = TransportDispatcher() if runner is None else runner
    # Single production enforcement point for the read-only Slurm allowlist.
    safe_runner = AllowlistedRunner(effective_runner)
    adapter = SlurmAdapter(safe_runner)
    now = effective_clock()
    clusters = _query_clusters_status(
        adapter,
        selected_entries,
        env=effective_env,
        now=now,
        refresh=parsed.refresh,
    )
    report = StatusReport(clusters=clusters)

    if parsed.json_output:
        out.write(format_status_json(report))
    else:
        out.write(format_status_table(report, now=now))
    return 0


def _query_clusters_status(
    adapter: SlurmAdapter,
    entries: Sequence[ClusterEntry],
    *,
    env: Mapping[str, str],
    now: datetime,
    refresh: bool = False,
) -> tuple[ClusterStatus, ...]:
    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        futures = [
            (
                entry,
                executor.submit(
                    cached_cluster_fetch,
                    entry,
                    command="status",
                    schema_version=StatusReport.schema_version,
                    result_type=ClusterStatus,
                    fetch=lambda target: adapter.query_status(target, now=now),
                    env=env,
                    now=now,
                    refresh=refresh,
                ),
            )
            for entry in entries
        ]
        results: list[ClusterStatus] = []
        for entry, future in futures:
            try:
                results.append(future.result())
            except AllowlistError:
                raise
            except Exception as exc:
                results.append(
                    ClusterStatus(
                        cluster=entry.name,
                        fetched_at=None,
                        stale=False,
                        user_scoped=False,
                        error=str(exc).strip() or type(exc).__name__,
                        partitions=(),
                    )
                )
    return tuple(results)

