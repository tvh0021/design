from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
import re
import sys
from typing import NoReturn, TextIO

from sglance.cache import cached_cluster_fetch
from sglance.config import ConfigError, load_config, validate_safe_token
from sglance.formatters import format_json, format_table
from sglance.models import (
    SCHEMA_VERSION,
    ClusterEntry,
    ClusterMine,
    ClusterQueue,
    ClusterResult,
    ClusterStatus,
    ClusterWait,
)
from sglance.runner import AllowlistError, CommandRunner, TransportDispatcher
from sglance.slurm import SlurmAdapter

_COMMAND_SUMMARIES = {
    "status": "Show partition CPU, GPU, and node utilization.",
    "queue": "Show running/pending job counts and top pending reasons.",
    "wait": "Show wait-time percentiles and oldest pending job age.",
    "mine": "Show your jobs or an account's jobs and start estimates.",
}


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _parse_days(raw: str) -> int:
    if not re.fullmatch(r"[+-]?[0-9]+", raw) or int(raw) < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1, got {raw!r}")
    return int(raw)


def _account_arg(raw: str) -> str:
    try:
        return validate_safe_token(raw, field_name="--account")
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_options(p: argparse.ArgumentParser, command: str | None = None) -> None:
    sup = argparse.SUPPRESS
    p.add_argument(
        "--cluster",
        default=None if command is None else sup,
        metavar="NAME",
        help="Query only the named configured cluster entry.",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        default=False if command is None else sup,
        help="Bypass the 5-minute cache and fetch fresh data.",
    )
    if command == "wait":
        p.add_argument(
            "--days",
            type=_parse_days,
            default=7,
            metavar="N",
            help="Lookback window in days (integer >= 1, default: 7).",
        )
    if command == "mine":
        p.add_argument(
            "--account",
            type=_account_arg,
            default=None,
            metavar="NAME",
            help="List all jobs charged to the named Slurm account.",
        )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=False if command is None else sup,
        help="Emit JSON output.",
    )


def _build_parser(stdout: TextIO, stderr: TextIO) -> argparse.ArgumentParser:
    class _CliParser(argparse.ArgumentParser):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.color = False

        def _print_message(self, message: str, file: TextIO | None = None) -> None:
            if message:
                (stdout if file is sys.stdout else stderr).write(message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            if message:
                self._print_message(message, sys.stderr)
            raise _ParserExit(status)

    parser = _CliParser(
        prog="sglance",
        description="Read-only inspection of Slurm cluster capacity, queues, wait times, and jobs.",
    )
    _add_options(parser)
    subparsers = parser.add_subparsers(dest="subcommand", required=True, parser_class=_CliParser)
    for name, summary in _COMMAND_SUMMARIES.items():
        _add_options(subparsers.add_parser(name, help=summary, description=summary), name)
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

    selected = tuple(e for e in cluster_entries if parsed.cluster in (None, e.name))
    if not selected:
        configured = ", ".join(e.name for e in cluster_entries)
        err.write(f"sglance: error: unknown cluster {parsed.cluster!r} (configured: {configured})\n")
        return 2

    adapter = SlurmAdapter(TransportDispatcher() if runner is None else runner)
    now = effective_clock()
    command: str = parsed.subcommand
    result_type, query = {
        "status": (ClusterStatus, adapter.query_status),
        "queue": (ClusterQueue, adapter.query_queue),
        "wait": (ClusterWait, adapter.query_wait),
        "mine": (ClusterMine, adapter.query_mine),
    }[command]
    extra: dict[str, int | str | None] = (
        {"days": parsed.days}
        if command == "wait"
        else {"account": parsed.account}
        if command == "mine"
        else {}
    )
    err_extra = {"account_filter": parsed.account} if command == "mine" else {}

    def query_one(entry: ClusterEntry) -> ClusterResult:
        try:
            return cached_cluster_fetch(
                entry,
                command=command,
                schema_version=SCHEMA_VERSION,
                result_type=result_type,  # type: ignore[arg-type]
                fetch=lambda t: query(t, now=now, **extra),  # type: ignore[operator]
                env=effective_env,
                now=now,
                refresh=parsed.refresh,
                options=extra,
            )
        except AllowlistError:
            raise
        except Exception as exc:
            return result_type(
                cluster=entry.name,
                error=str(exc).strip() or type(exc).__name__,
                **err_extra,  # type: ignore[arg-type]
            )

    with ThreadPoolExecutor(max_workers=len(selected)) as ex:
        clusters = list(ex.map(query_one, selected))
    out.write(
        format_json(command, clusters, extra)
        if parsed.json_output
        else format_table(command, clusters, now)
    )
    return 0

