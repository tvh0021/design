from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import re

from sglance._parse import is_gpu_type, iter_rows, parse_epoch
from sglance.models import MineJob

MINE_SQUEUE_FIELDS = (
    "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
    "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
    "StartTime:0|,Reason:0|,Name:0|"
)

_PENDING_STATES = frozenset({"PD", "RQ", "RH", "RD", "RF", "SE"})
_VALID_NONTERMINAL_STATES = _PENDING_STATES | {
    "R", "CF", "CG", "S", "ST", "RS", "SI", "SO", "PO"
}
_INVALID_GPU_TYPES = frozenset({"", "(null)", "none", "n/a", "invalid", "no_consume"})
_INVALID_PARTITION_VALUES = frozenset({"", "(null)", "n/a", "none"})
_JOB_ID_RE = re.compile(r"^\d+(?:_\d+|\+\d+|_\[[0-9][0-9,\-:%]*\])?$")
_DURATION_RE = re.compile(r"(?:(?:([0-9]+)-)?([0-9]+):)?([0-9]+):([0-9]+)")


def _parse_optional(parser: Callable[[str], int], raw: str) -> int | None:
    try:
        return parser(raw)
    except ValueError:
        return None


def _parse_nonneg_int(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(raw)
    return int(raw)


def _parse_duration(raw: str) -> int:
    m = _DURATION_RE.fullmatch(raw.strip())
    if not m:
        raise ValueError(raw)
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    if seconds >= 60 or (m[2] is not None and minutes >= 60):
        raise ValueError(raw)
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _parse_tres_spec_gpus(raw: str) -> int | None:
    untyped: list[int] = []
    typed: list[int] = []
    for token in raw.split(","):
        item = token.strip()
        low = item.lower()
        if low.startswith(("gres/gpu", "gres:gpu")):
            rest = item[8:].strip()
        elif low.startswith("gpu"):
            rest = item[3:].strip()
        else:
            continue
        if not rest:
            untyped.append(1)
        elif rest[0] == "=":
            untyped.append(_parse_nonneg_int(rest[1:].strip()))
        elif rest[0] == ":":
            tail = rest[1:].strip()
            parts = [p.strip() for p in (tail.partition("=")[::2] if "=" in tail else tail.split(":"))]
            if len(parts) == 1 and parts[0].isascii() and parts[0].isdigit():
                untyped.append(int(parts[0]))
            elif len(parts) <= 2 and is_gpu_type(parts[0], _INVALID_GPU_TYPES):
                typed.append(_parse_nonneg_int(parts[1]) if len(parts) == 2 else 1)
            else:
                raise ValueError(raw)
    return sum(untyped) if untyped else sum(typed) if typed else None


def _resolve_job_gpus(
    alloc: str, per_node: str, per_job: str, per_task: str, *, nodes: int | None, tasks: int | None
) -> int | None:
    try:
        for raw, factor in ((alloc, 1), (per_job, 1), (per_node, nodes), (per_task, tasks)):
            count = _parse_tres_spec_gpus(raw)
            if count is not None:
                return None if factor is None else count * factor
    except ValueError:
        return None
    return 0


def _normalize_partition(raw: str) -> str:
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part.rstrip("*") for part in parts):
        return ""
    first = parts[0].removesuffix("*")
    if (
        first.lower() in _INVALID_PARTITION_VALUES
        or first.startswith("-")
        or any(ch.isspace() for ch in first)
    ):
        return ""
    return first


def _parse_estimated_start(raw: str, now_utc: datetime) -> str | None:
    epoch = parse_epoch(raw)
    if epoch is None:
        return None
    try:
        start_dt = datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return start_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if start_dt >= now_utc else None


def parse_squeue_mine(
    stdout: str, *, now: datetime
) -> tuple[tuple[MineJob, ...], frozenset[str], str | None]:
    now_utc = now.astimezone(UTC)
    jobs: list[MineJob] = []
    users: set[str] = set()
    first_user: str | None = None

    for _line, cells in iter_rows(stdout, "squeue", 17, maxsplit=16):
        (
            job_id, user_name, account_val, partition_field, state,
            time_used, time_limit, num_nodes, num_cpus, num_tasks,
            tres_alloc, tres_per_node, tres_per_job, tres_per_task,
            start_time, reason_raw, name,
        ) = cells
        partition = _normalize_partition(partition_field)
        if not _JOB_ID_RE.fullmatch(job_id) or not partition or state not in _VALID_NONTERMINAL_STATES:
            continue

        nodes = _parse_optional(_parse_nonneg_int, num_nodes)
        cpus = _parse_optional(_parse_nonneg_int, num_cpus)
        tasks = _parse_optional(_parse_nonneg_int, num_tasks)
        is_pending = state in _PENDING_STATES
        if user_name:
            users.add(user_name)
            if first_user is None:
                first_user = user_name

        jobs.append(
            MineJob(
                job_id=job_id, name=name, user=user_name, account=account_val,
                partition=partition, state=state, cpus=cpus,
                gpus=_resolve_job_gpus(
                    tres_alloc, tres_per_node, tres_per_job, tres_per_task,
                    nodes=nodes, tasks=tasks,
                ),
                nodes=nodes,
                elapsed_seconds=_parse_optional(_parse_duration, time_used),
                time_limit_seconds=_parse_optional(_parse_duration, time_limit),
                reason=(reason_raw or "None") if is_pending else None,
                estimated_start=_parse_estimated_start(start_time, now_utc) if is_pending else None,
            )
        )
    return tuple(jobs), frozenset(users), first_user
