from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
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

    cluster_help = "Query only the named configured cluster entry."
    refresh_help = "Bypass the 5-minute cache and fetch fresh data."
    json_help = "Emit JSON output."

    parser = _CliParser(
        prog="sglance",
        description="Read-only inspection of Slurm cluster capacity, queues, wait times, and jobs.",
    )
    parser.add_argument(
        "--cluster",
        dest="cluster",
        default=None,
        metavar="NAME",
        help=cluster_help,
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=False,
        help=refresh_help,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=False,
        help=json_help,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True, parser_class=_CliParser)

    status_summary = "Show partition CPU, GPU, and node utilization."
    status_parser = subparsers.add_parser(
        "status",
        help=status_summary,
        description=status_summary,
    )
    status_parser.add_argument(
        "--cluster",
        dest="cluster",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help=cluster_help,
    )
    status_parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=argparse.SUPPRESS,
        help=refresh_help,
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help=json_help,
    )

    queue_summary = "Show running/pending job counts and top pending reasons."
    queue_parser = subparsers.add_parser(
        "queue",
        help=queue_summary,
        description=queue_summary,
    )
    queue_parser.add_argument(
        "--cluster",
        dest="cluster",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help=cluster_help,
    )
    queue_parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=argparse.SUPPRESS,
        help=refresh_help,
    )
    queue_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help=json_help,
    )

    def _parse_days(raw: str) -> int:
        has_digits = (
            raw[1:].isascii() and raw[1:].isdigit()
            if raw[:1] in {"+", "-"}
            else raw.isascii() and raw.isdigit()
        )
        if not raw or not has_digits:
            raise argparse.ArgumentTypeError(f"must be an integer >= 1, got {raw!r}")
        value = int(raw, 10)
        if value < 1:
            raise argparse.ArgumentTypeError(f"must be an integer >= 1, got {raw!r}")
        return value

    wait_summary = "Show wait-time percentiles and oldest pending job age."
    wait_parser = subparsers.add_parser(
        "wait",
        help=wait_summary,
        description=wait_summary,
    )
    wait_parser.add_argument(
        "--cluster",
        dest="cluster",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help=cluster_help,
    )
    wait_parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=argparse.SUPPRESS,
        help=refresh_help,
    )
    wait_parser.add_argument(
        "--days",
        type=_parse_days,
        default=7,
        metavar="N",
        help="Lookback window in days (integer >= 1, default: 7).",
    )
    wait_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help=json_help,
    )

    def _account_arg(raw: str) -> str:
        try:
            return validate_safe_token(raw, field_name="--account")
        except ConfigError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    mine_summary = "Show your jobs or an account's jobs and start estimates."
    mine_parser = subparsers.add_parser(
        "mine",
        help=mine_summary,
        description=mine_summary,
    )
    mine_parser.add_argument(
        "--cluster",
        dest="cluster",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help=cluster_help,
    )
    mine_parser.add_argument(
        "--refresh",
        action="store_true",
        dest="refresh",
        default=argparse.SUPPRESS,
        help=refresh_help,
    )
    mine_parser.add_argument(
        "--account",
        dest="account",
        type=_account_arg,
        default=None,
        metavar="NAME",
        help="List all jobs charged to the named Slurm account.",
    )
    mine_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help=json_help,
    )
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

