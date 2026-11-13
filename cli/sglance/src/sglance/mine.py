from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import re

from sglance.models import MineJob

MINE_SQUEUE_FIELDS = (
    "JobID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,"
    "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,StartTime:0|,Reason:0|,Name:0|"
)

_PENDING_STATES = frozenset({"PD", "RQ", "RH", "RD", "RF", "SE"})
_ACTIVE_STATES = frozenset({"R", "CF", "CG", "S", "ST", "RS", "SI", "SO", "PO"})
_VALID_NONTERMINAL_STATES = _PENDING_STATES | _ACTIVE_STATES
_UNBOUNDED_LIMITS = frozenset({"unlimited", "partition_limit"})
_UNSET_START_VALUES = frozenset({"", "n/a", "unknown", "none", "(null)"})
_UNSET_TRES_VALUES = frozenset({"", "(null)", "none", "n/a"})
_INVALID_GPU_TYPES = frozenset({"", "(null)", "none", "n/a", "invalid", "no_consume"})
_INVALID_PARTITION_VALUES = frozenset({"", "(null)", "n/a", "none"})
_JOB_ID_RE = re.compile(r"^\d+(?:_\d+|\+\d+|_\[[0-9][0-9,\-:%]*\])?$")
_GPU_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class MineParseError(Exception):
    """Raised when squeue output for mine is structurally malformed."""


def _parse_optional(parser: Callable[[str], int | None], raw: str) -> int | None:
    try:
        return parser(raw)
    except ValueError:
        return None


def _parse_nonneg_int(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"invalid non-negative integer {raw!r}")
    return int(raw)


def _parse_slurm_duration_seconds(raw: str) -> int:
    text = raw.strip()
    if not text:
        raise ValueError("empty duration")
    days = 0
    if "-" in text:
        days_part, time_part = text.split("-", 1)
        days = _parse_nonneg_int(days_part)
    else:
        time_part = text
    parts = time_part.split(":")
    if len(parts) == 2 and "-" not in text:
        minutes = _parse_nonneg_int(parts[0])
        seconds = _parse_nonneg_int(parts[1])
        if seconds >= 60:
            raise ValueError(f"invalid seconds in {raw!r}")
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = _parse_nonneg_int(parts[0])
        minutes = _parse_nonneg_int(parts[1])
        seconds = _parse_nonneg_int(parts[2])
        if minutes >= 60 or seconds >= 60:
            raise ValueError(f"invalid MM:SS in {raw!r}")
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"invalid duration {raw!r}")


def _parse_time_limit_seconds(raw: str) -> int | None:
    stripped = raw.strip()
    if stripped.lower() in _UNBOUNDED_LIMITS:
        return None
    return _parse_slurm_duration_seconds(stripped)


def _is_gpu_type_name(value: str) -> bool:
    lowered = value.lower()
    if lowered in _INVALID_GPU_TYPES or re.fullmatch(r"\d+[kmgtp]", lowered):
        return False
    return bool(_GPU_TYPE_RE.fullmatch(value)) and any(
        ch.isascii() and ch.isalpha() for ch in value
    )


def _parse_tres_spec_gpus(raw: str) -> tuple[bool, int]:
    stripped = raw.strip()
    if stripped.lower() in _UNSET_TRES_VALUES:
        return False, 0
    has_untyped = False
    untyped_gpus = 0
    has_typed = False
    typed_gpus = 0
    for token in stripped.split(","):
        item = token.strip()
        if not item:
            continue
        item_lower = item.lower()
        if item_lower.startswith(("gres/gpu", "gres:gpu")):
            rest = item[8:].strip()
        elif item_lower.startswith("gpu"):
            rest = item[3:].strip()
        else:
            continue
        if rest and rest[0] not in {":", "="}:
            continue
        if not rest:
            has_untyped = True
            untyped_gpus += 1
        elif rest[0] == "=":
            untyped_gpus += _parse_nonneg_int(rest[1:].strip())
            has_untyped = True
        else:
            tail = rest[1:].strip()
            if not tail:
                raise ValueError(f"empty GPU specifier in TRES {raw!r}")
            if "=" in tail:
                gpu_type, _, count_str = tail.partition("=")
                if not _is_gpu_type_name(gpu_type.strip()):
                    raise ValueError(f"invalid GPU type in TRES {raw!r}")
                typed_gpus += _parse_nonneg_int(count_str.strip())
                has_typed = True
            else:
                parts = [part.strip() for part in tail.split(":")]
                if any(not part for part in parts):
                    raise ValueError(f"malformed GPU TRES {raw!r}")
                if len(parts) == 1:
                    if parts[0].isascii() and parts[0].isdigit():
                        untyped_gpus += int(parts[0])
                        has_untyped = True
                    elif _is_gpu_type_name(parts[0]):
                        typed_gpus += 1
                        has_typed = True
                    else:
                        raise ValueError(f"malformed GPU TRES {raw!r}")
                elif len(parts) == 2:
                    if not _is_gpu_type_name(parts[0]):
                        raise ValueError(f"invalid GPU type in TRES {raw!r}")
                    typed_gpus += _parse_nonneg_int(parts[1])
                    has_typed = True
                else:
                    raise ValueError(f"malformed GPU TRES {raw!r}")
    has_any = has_untyped or has_typed
    return has_any, (untyped_gpus if has_untyped else typed_gpus)


def _resolve_job_gpus(
    tres_alloc_raw: str,
    tres_per_node_raw: str,
    tres_per_job_raw: str,
    *,
    nodes: int | None,
) -> int | None:
    try:
        alloc_has_gpu, alloc_gpus = _parse_tres_spec_gpus(tres_alloc_raw)
    except ValueError:
        return None
    if alloc_has_gpu:
        return alloc_gpus
    try:
        per_job_has_gpu, per_job_gpus = _parse_tres_spec_gpus(tres_per_job_raw)
        per_node_has_gpu, per_node_gpus = _parse_tres_spec_gpus(tres_per_node_raw)
    except ValueError:
        return None
    if per_job_has_gpu:
        return per_job_gpus
    if per_node_has_gpu:
        if nodes is None:
            return None
        return per_node_gpus * nodes
    return 0


def _normalize_partition(raw: str) -> str:
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part.rstrip("*") for part in parts):
        return ""
    first = parts[0]
    if first.endswith("*"):
        first = first[:-1]
    if (
        first.lower() in _INVALID_PARTITION_VALUES
        or first.startswith("-")
        or any(ch.isspace() for ch in first)
    ):
        return ""
    return first


def _parse_estimated_start(raw: str, *, is_pending: bool, now_utc: datetime) -> str | None:
    if not is_pending:
        return None
    stripped = raw.strip()
    if stripped.lower() in _UNSET_START_VALUES:
        return None
    if not stripped.isascii() or not stripped.isdigit():
        return None
    epoch = int(stripped)
    if epoch <= 0:
        return None
    try:
        start_dt = datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if start_dt < now_utc:
        return None
    return start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_squeue_mine(
    stdout: str, *, now: datetime
) -> tuple[tuple[MineJob, ...], frozenset[str], str | None]:
    now_utc = now.astimezone(UTC)
    jobs: list[MineJob] = []
    distinct_users: set[str] = set()
    first_user: str | None = None

    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("CLUSTER: "):
            continue
        if not stripped.endswith("|"):
            raise MineParseError(f"malformed squeue output: {stripped}")
        cells = [cell.strip() for cell in stripped[:-1].split("|", 14)]
        if len(cells) != 15:
            raise MineParseError(f"malformed squeue output: {stripped}")

        (
            job_id,
            user_name,
            account_val,
            partition_field,
            state_compact,
            time_used_raw,
            time_limit_raw,
            num_nodes_raw,
            num_cpus_raw,
            tres_alloc_raw,
            tres_per_node_raw,
            tres_per_job_raw,
            start_time_raw,
            reason_raw,
            name,
        ) = cells

        partition_name = _normalize_partition(partition_field)
        if (
            not _JOB_ID_RE.fullmatch(job_id)
            or not partition_name
            or state_compact not in _VALID_NONTERMINAL_STATES
        ):
            continue

        nodes = _parse_optional(_parse_nonneg_int, num_nodes_raw)
        cpus = _parse_optional(_parse_nonneg_int, num_cpus_raw)
        elapsed_seconds = _parse_optional(_parse_slurm_duration_seconds, time_used_raw)
        time_limit_seconds = _parse_optional(_parse_time_limit_seconds, time_limit_raw)
        gpus = _resolve_job_gpus(
            tres_alloc_raw, tres_per_node_raw, tres_per_job_raw, nodes=nodes
        )

        is_pending = state_compact in _PENDING_STATES
        reason = (reason_raw or "None") if is_pending else None
        estimated_start = _parse_estimated_start(
            start_time_raw, is_pending=is_pending, now_utc=now_utc
        )

        if user_name:
            distinct_users.add(user_name)
            if first_user is None:
                first_user = user_name

        jobs.append(
            MineJob(
                job_id=job_id,
                name=name,
                user=user_name,
                account=account_val,
                partition=partition_name,
                state=state_compact,
                cpus=cpus,
                gpus=gpus,
                nodes=nodes,
                elapsed_seconds=elapsed_seconds,
                time_limit_seconds=time_limit_seconds,
                reason=reason,
                estimated_start=estimated_start,
            )
        )

    return tuple(jobs), frozenset(distinct_users), first_user
