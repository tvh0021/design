from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
import os
import sys
from typing import NoReturn, TextIO

from sglance.config import ConfigError, load_config
from sglance.formatters import format_status_json, format_status_table
from sglance.models import StatusReport
from sglance.runner import AllowlistedRunner, CommandRunner
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
    subparsers = parser.add_subparsers(dest="subcommand", required=True, parser_class=_CliParser)
    status_parser = subparsers.add_parser("status", help="Show partition CPU, GPU, and node utilization.")
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

    if runner is None:
        err.write("sglance: error: no transport adapter available yet\n")
        return 2

    # Single production enforcement point for the read-only Slurm allowlist.
    safe_runner = AllowlistedRunner(runner)
    adapter = SlurmAdapter(safe_runner)
    now = effective_clock()
    clusters = tuple(adapter.query_status(entry, now=now) for entry in cluster_entries)
    report = StatusReport(clusters=clusters)

    if parsed.json_output:
        out.write(format_status_json(report))
    else:
        out.write(format_status_table(report))
    return 0
